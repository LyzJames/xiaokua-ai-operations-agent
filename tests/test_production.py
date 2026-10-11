import base64
import copy
import hashlib
import hmac
import json
import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,update
from backend.production.api import create_app
from backend.production.security import Settings,Vault,password_hash,shop_domain,body_hmac
from backend.production.database import (connections,actions,tasks,users,resources,jobs,events,tenants,sessions,audits,clock,uid)
from backend.production.workflows import BusinessError,cents
from backend.production.shopify import Shopify,ShopifyError
from backend.production.feishu import verify_event,FeishuError

class FakeShop:
    def __init__(self):
        self.o={"id":"gid://shopify/Order/1","name":"#TEST1","test":True,"tags":["xiaokua-test"],"cancelledAt":None,"currencyCode":"USD","displayFinancialStatus":"PAID","displayFulfillmentStatus":"UNFULFILLED",
            "totalPriceSet":{"shopMoney":{"amount":"10.00","currencyCode":"USD"}},"totalRefundedSet":{"shopMoney":{"amount":"0.00","currencyCode":"USD"}},"netPaymentSet":{"shopMoney":{"amount":"10.00","currencyCode":"USD"}},
            "transactions":[{"id":"gid://shopify/OrderTransaction/1","kind":"SALE","status":"SUCCESS","gateway":"bogus","test":True,"amountSet":{"shopMoney":{"amount":"10.00","currencyCode":"USD"}},"parentTransaction":None}],
            "fulfillments":[],"fulfillmentOrders":{"nodes":[],"pageInfo":{"hasNextPage":False}}}
        self.quantity=10;self.description="<p>Original</p>";self.tags=["xiaokua-test"];self.calls=[];self.unknown=False
    def capability(self): return {"shop":{"name":"Test","plan":{"partnerDevelopment":True}},"currentAppInstallation":{"accessScopes":[{"handle":"write_orders"}]}}
    def orders(self,search=""):return [copy.deepcopy(self.o)]
    def variants(self):return []
    def order(self,id):return copy.deepcopy(self.o)
    def inventory(self,item,loc):return {"variant":{"product":{"tags":self.tags}},"inventoryLevel":{"quantities":[{"quantity":self.quantity}]}}
    def product(self,id):return {"id":id,"descriptionHtml":self.description,"tags":self.tags}
    def inventory_set(self,item,loc,before,q,key):
        self.calls.append(("inventory",key));self.quantity=q
        if self.unknown:raise ShopifyError("TRANSPORT_UNKNOWN",True)
        return {"inventoryAdjustmentGroup":{"changes":[]},"userErrors":[]}
    def refund(self,oid,amount,currency,pid,gateway,key):
        self.calls.append(("refund",key))
        return {"refund":{"id":"gid://shopify/Refund/1","transactions":{"nodes":[{"status":"SUCCESS"}]}},"userErrors":[]}
    def product_update(self,pid,html):self.calls.append(("product",pid));self.description=html;return {"product":{"id":pid},"userErrors":[]}
    def fulfill(self,o,p):self.calls.append(("fulfill",o["id"]));return {"fulfillment":{"id":"gid://shopify/Fulfillment/1"},"userErrors":[]}
    def cancel(self,oid,r):self.calls.append(("cancel",oid));return {"job":{"id":"gid://shopify/Job/1","done":True},"userErrors":[]}

@pytest.fixture
def product(tmp_path):
    s=Settings(tmp_path,"sqlite:///"+str(tmp_path/'product.sqlite'),public_url="http://testserver",allow_registration=True,worker_enabled=False)
    fake=FakeShop();app=create_app(s,lambda c,t:fake)
    client=TestClient(app)
    with client:
        yield app,client,fake

def csrf(client): return client.get('/api/bootstrap').json()['csrf']
def post(client,path,body):return client.post('/api'+path,json=body,headers={'X-CSRF-Token':csrf(client)})
def register(app,client,email='owner@example.com'):
    token=(app.state.settings.directory/'setup.token').read_text()
    r=post(client,'/auth/register',{'email':email,'password':'example-password-123','name':'Owner','organization':email,'setup_token':token})
    assert r.status_code==201,r.text
    return r.json()['user']
def connect(app,user):
    cid=uid('conn')
    with app.state.db.transaction() as db:
        db.execute(connections.insert().values(id=cid,tenant_id=user['tenant_id'],provider='shopify',external_id=cid.replace('_','-')+'.myshopify.com',secret=app.state.vault.seal({'client_secret':'private','access_token':'private-token'}),status='active',info={},created_at=clock()))
    return cid
def propose(app,user,cid,kind='inventory',params=None):
    p=params or {'item_id':'gid://shopify/InventoryItem/1','location_id':'gid://shopify/Location/1','quantity':20}
    task=app.state.ops.submit(user,'Test '+kind,kind,p,cid)
    app.state.ops.process_task(task['id'],user['tenant_id'])
    return app.state.db.list(actions,user['tenant_id'],1,task_id=task['id'])[0]
def other_approver(app,user):
    u=dict(id=uid('user'),tenant_id=user['tenant_id'],email=uid('mail')+'@example.com',name='Approver',password=password_hash('example-approval-123'),role='approver',active=True,feishu_open_id=None,created_at=clock())
    with app.state.db.transaction() as db:db.execute(users.insert().values(**u))
    return u

def test_auth_session_csrf_and_plaintext_secrets(product):
    app,c,f=product
    assert c.get('/api/state').status_code==401
    assert c.post('/api/auth/login',json={'email':'unknown@example.invalid','password':'wrong'}).status_code==403
    u=register(app,c);connect(app,u)
    state=c.get('/api/state');assert state.status_code==200
    assert 'private-token' not in state.text and 'secret' not in state.text and 'password' not in state.text
    assert c.post('/api/tasks',json={'command':'test'}).status_code==403
    assert c.post('/api/tasks',json={'command':'test'},headers={'X-CSRF-Token':csrf(c),'Origin':'https://evil.example'}).status_code==403
    assert state.headers['X-Frame-Options']=='DENY'

def test_tenant_isolation_and_foreign_connection(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u)
    a=propose(app,u,cid)
    post(c,'/auth/logout',{})
    v=register(app,c,'second@example.com')
    assert c.get('/api/state').json()['actions']==[]
    assert c.get('/api/tasks/'+a['task_id']).status_code==404
    assert post(c,'/tasks',{'command':'foreign','kind':'sync','connection_id':cid}).status_code==422
    assert post(c,'/approvals/'+a['id'],{'approved':True}).status_code==422
    assert app.state.db.verify(u['tenant_id'])['valid']
    assert app.state.db.verify(v['tenant_id'])['valid']

def test_independent_approval_and_idempotent_execute(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid)
    assert post(c,'/approvals/'+a['id'],{'approved':True}).status_code==422
    approver=other_approver(app,u);app.state.ops.decide(approver,a['id'],True,'reviewed')
    app.state.ops.execute(a['id'],u['tenant_id']);app.state.ops.execute(a['id'],u['tenant_id'])
    assert len(f.calls)==1 and f.quantity==20
    assert app.state.db.get(actions,a['id'],u['tenant_id'])['status']=='executed'
    assert app.state.db.verify(u['tenant_id'])['valid']

def test_approver_permission_revoked_before_execution(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid);v=other_approver(app,u)
    app.state.ops.decide(v,a['id'],True)
    with app.state.db.transaction() as db:db.execute(update(users).where(users.c.id==v['id']).values(active=False))
    with pytest.raises(BusinessError):app.state.ops.execute(a['id'],u['tenant_id'])
    assert f.calls==[]

def test_inventory_stale_and_unknown_result_no_retry(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid);v=other_approver(app,u)
    app.state.ops.decide(v,a['id'],True);f.quantity=11
    with pytest.raises(BusinessError):app.state.ops.execute(a['id'],u['tenant_id'])
    assert f.calls==[]
    a=propose(app,u,cid);app.state.ops.decide(v,a['id'],True);f.unknown=True
    with pytest.raises(ShopifyError):app.state.ops.execute(a['id'],u['tenant_id'])
    with pytest.raises(BusinessError):app.state.ops.execute(a['id'],u['tenant_id'])
    assert len(f.calls)==1
    assert app.state.db.get(actions,a['id'],u['tenant_id'])['status']=='uncertain'

@pytest.mark.parametrize('kind',['refund','cancel','fulfill'])
def test_live_order_guard(product,kind):
    app,c,f=product;u=register(app,c);cid=connect(app,u);f.o['test']=False
    with pytest.raises(BusinessError):propose(app,u,cid,kind,{'order_id':f.o['id'],'amount':'1.00','number':'TEST','company':'Test'})
    assert not f.calls

def test_test_order_cannot_refund_non_test_transaction(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);f.o['transactions'][0]['test']=False
    with pytest.raises(BusinessError):propose(app,u,cid,'refund',{'order_id':f.o['id'],'amount':'1.00'})
    f.o['transactions'][0]['test']=True
    with pytest.raises(BusinessError):propose(app,u,cid,'refund',{'order_id':f.o['id'],'amount':'10.01'})
    a=propose(app,u,cid,'refund',{'order_id':f.o['id'],'amount':'1.00'})
    v=other_approver(app,u);app.state.ops.decide(v,a['id'],True);app.state.ops.execute(a['id'],u['tenant_id']);assert f.calls[0][0]=='refund'

def test_action_expiration(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid)
    with app.state.db.transaction() as db:db.execute(update(actions).where(actions.c.id==a['id']).values(expires_at=clock()-1))
    with pytest.raises(BusinessError):app.state.ops.decide(other_approver(app,u),a['id'],True)
    app.state.worker.recover();assert app.state.db.get(actions,a['id'],u['tenant_id'])['status']=='expired'

def test_worker_crash_marks_result_uncertain(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid);v=other_approver(app,u);app.state.ops.decide(v,a['id'],True)
    with app.state.db.transaction() as db:
        db.execute(update(actions).where(actions.c.id==a['id']).values(status='executing'))
        db.execute(update(jobs).where(jobs.c.kind=='action',jobs.c.target_id==a['id']).values(status='running',lease_until=clock()-1))
    app.state.worker.recover()
    assert app.state.db.get(actions,a['id'],u['tenant_id'])['status']=='uncertain'
    assert f.calls==[]

def test_invite_role_viewer_and_last_owner_guard(product):
    app,c,f=product;u=register(app,c)
    assert post(c,'/members/'+u['id'],{'role':'viewer'}).status_code==422
    inv=post(c,'/members/invite',{'email':'viewer@example.com','role':'viewer'}).json()['url'].split('invite=')[1]
    post(c,'/auth/logout',{})
    r=post(c,'/auth/invite/accept',{'email':'viewer@example.com','password':'example-viewer-123','name':'Viewer','token':inv});assert r.status_code==201
    assert post(c,'/tasks',{'command':'unauthorized'}).status_code==403
    assert post(c,'/members/invite',{'email':'unknown@example.invalid','role':'owner'}).status_code==403
    assert c.get('/api/state').json()['members']==[]

def test_shopify_webhook_hmac_duplicate_and_pii_minimization(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);conn=app.state.db.get(connections,cid,u['tenant_id'])
    body=json.dumps({'id':1,'email':'private-customer@example.com'}).encode()
    headers={'X-Shopify-Shop-Domain':conn['external_id'],'X-Shopify-Topic':'orders/create','X-Shopify-Event-Id':'event1','X-Shopify-Hmac-Sha256':base64.b64encode(hmac.new(b'private',body,hashlib.sha256).digest()).decode()}
    assert c.post('/hooks/shopify',content=body,headers={**headers,'X-Shopify-Hmac-Sha256':'bad'}).status_code==401
    assert c.post('/hooks/shopify',content=body,headers=headers).status_code==200
    assert c.post('/hooks/shopify',content=body,headers=headers).json()['duplicate']
    ev=app.state.db.list(events)[0];assert ev['payload']=={}
    assert 'private-customer' not in c.get('/api/state').text

def test_feishu_signed_callback_and_sender_binding(product):
    app,c,f=product;u=register(app,c)
    secret={'app_id':'cli_test','app_secret':'private','verification_token':'verification','encrypt_key':'encryption'}
    value={'schema':'2.0','header':{'event_id':'e1','event_type':'im.message.receive_v1','app_id':'cli_test','token':'verification'},'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_unauthorized'}},'message':{'message_type':'text','message_id':'m1','content':'{"text":"同步订单"}'}}}
    body=json.dumps(value).encode();ts=str(int(clock()));nonce='nonce';headers={'x-lark-request-timestamp':ts,'x-lark-request-nonce':nonce,'x-lark-signature':hashlib.sha256((ts+nonce+'encryption').encode()+body).hexdigest()}
    assert verify_event(body,headers,secret)==value
    with pytest.raises(FeishuError):verify_event(body,{**headers,'x-lark-signature':'bad'},secret)
    conn={'id':uid('conn'),'tenant_id':u['tenant_id'],'info':{}}
    with pytest.raises(FeishuError):app.state.feishu.receive(conn,value)

def test_feishu_websocket_configuration_has_no_unsigned_http_entry(product,monkeypatch):
    import backend.production.api as routes
    app,c,f=product;u=register(app,c)
    monkeypatch.setattr(routes,'tenant_token',lambda secret:'test-token')
    monkeypatch.setattr(routes,'feishu_request',lambda *args,**kwargs:{'code':0,'bot':{'activate_status':2}})
    body={'app_id':'cli_ws_test','app_secret':'local-secret-123456','delivery':'websocket'}
    r=post(c,'/feishu/connect',body);assert r.status_code==200
    cid=r.json()['id'];assert r.json()['callback_url'] is None
    con=app.state.db.get(connections,cid,u['tenant_id'])
    assert con['info']['delivery']=='websocket'
    assert app.state.vault.open(con['secret'])['app_secret']==body['app_secret']
    assert c.post('/hooks/feishu/'+cid,json={'header':{'event_type':'im.message.receive_v1'}}).status_code==404
    assert 'local-secret-123456' not in c.get('/api/state').text

def test_feishu_https_still_requires_both_callback_secrets(product,monkeypatch):
    import backend.production.api as routes
    app,c,f=product;register(app,c)
    monkeypatch.setattr(routes,'tenant_token',lambda secret:'test-token')
    monkeypatch.setattr(routes,'feishu_request',lambda *args,**kwargs:{'code':0})
    body={'app_id':'cli_https_test','app_secret':'local-secret-123456'}
    assert post(c,'/feishu/connect',body).status_code==422
    assert post(c,'/feishu/connect',{**body,'delivery':'invalid'}).status_code==422
    assert post(c,'/feishu/connect',{**body,'verification_token':'verification-123','encrypt_key':'encrypt-key-123'}).status_code==200

def test_audit_append_only_and_concurrent_chain(product):
    app,c,f=product;u=register(app,c)
    def append(i):
        with app.state.db.transaction() as db:app.state.db.audit(db,u['tenant_id'],u['id'],'concurrent.test',str(i),{})
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(append,range(12)))
    assert app.state.db.verify(u['tenant_id'])['valid']
    with pytest.raises(Exception):
        with app.state.db.transaction() as db:db.execute(update(audits).values(event='tampered'))

def test_password_change_and_logout(product):
    app,c,f=product;u=register(app,c)
    token=c.cookies.get('xiaokua_session')
    assert post(c,'/auth/password',{'current_password':'example-password-123','new_password':'new-password-1234'}).status_code==200
    post(c,'/auth/logout',{})
    assert post(c,'/auth/login',{'email':u['email'],'password':'example-password-123'}).status_code==401
    assert post(c,'/auth/login',{'email':u['email'],'password':'new-password-1234'}).status_code==200

def test_session_tasks_and_vault_survive_new_app_instance(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid)
    restarted=create_app(app.state.settings,lambda cid,t:f)
    with TestClient(restarted) as c2:
        c2.cookies.update(c.cookies)
        state=c2.get('/api/state').json()
        assert state['user']['id']==u['id'] and state['actions'][0]['id']==a['id']
        con=restarted.state.db.get(connections,cid,u['tenant_id'])
        assert restarted.state.vault.open(con['secret'])['access_token']=='private-token'
        assert state['audit_verification']['valid']

def test_partial_sync_preserves_product_read_and_can_retry(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u)
    def denied(search=''):raise ShopifyError('ACCESS_DENIED')
    f.orders=denied
    task=post(c,'/tasks',{'command':'sync','kind':'sync','connection_id':cid}).json()
    app.state.ops.process_task(task['id'],u['tenant_id'])
    observed=app.state.db.get(tasks,task['id'],u['tenant_id'])
    assert observed['status']=='partial' and observed['result']['errors']==[{'component':'orders','code':'ACCESS_DENIED'}]
    assert post(c,'/tasks/'+task['id']+'/retry',{}).status_code==200

def test_import_learning_has_evidence_redaction_and_no_duplicate(product):
    app,c,f=product;u=register(app,c)
    records=[{'id':'t'+str(i),'market':'de','text':'energy efficiency? contact customer@example.com'} for i in range(5)]
    assert post(c,'/support/import',{'records':records}).status_code==200
    assert post(c,'/support/import',{'records':records}).status_code==200
    assert len(app.state.db.list(resources,u['tenant_id'],kind='ticket'))==5
    for i in range(2):
        task=app.state.ops.submit(u,'learn','learn',{})
        app.state.ops.process_task(task['id'],u['tenant_id'])
    state=c.get('/api/state').json();assert len(state['knowledge'])==1
    k=state['knowledge'][0];assert not k['active'] and len(k['evidence'])==5
    assert 'customer@example.com' not in json.dumps(k)
    assert c.get('/api/knowledge/'+k['id']+'/download').status_code==200
    assert c.get('/api/tasks/'+task['id']+'/report').status_code==200

def test_uncertain_reconciliation_reads_provider_without_rewriting(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid);v=other_approver(app,u)
    app.state.ops.decide(v,a['id'],True);f.unknown=True
    with pytest.raises(ShopifyError):app.state.ops.execute(a['id'],u['tenant_id'])
    assert app.state.ops.reconcile(v,a['id'],'platform checked')['status']=='reconciled'
    app.state.ops.execute(a['id'],u['tenant_id']);assert len(f.calls)==1
    assert app.state.db.verify(u['tenant_id'])['valid']

def test_uncertain_reconcile_rejects_unmatched_result(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid);v=other_approver(app,u)
    with app.state.db.transaction() as db:db.execute(update(actions).where(actions.c.id==a['id']).values(status='uncertain'))
    with pytest.raises(BusinessError):app.state.ops.reconcile(v,a['id'],'not confirmed')
    assert f.calls==[] and app.state.db.get(actions,a['id'],u['tenant_id'])['status']=='uncertain'

def test_tracking_owner_and_stale_tracking_guard(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u)
    f.o['fulfillments']=[{'id':'gid://shopify/Fulfillment/1','status':'SUCCESS','trackingInfo':[{'number':'old','company':'DHL'}]}]
    params={'order_id':f.o['id'],'fulfillment_id':'gid://shopify/Fulfillment/2','company':'DHL','number':'new'}
    with pytest.raises(BusinessError):propose(app,u,cid,'tracking',params)
    params['fulfillment_id']='gid://shopify/Fulfillment/1';a=propose(app,u,cid,'tracking',params)
    v=other_approver(app,u);app.state.ops.decide(v,a['id'],True)
    f.o['fulfillments'][0]['trackingInfo'][0]['number']='externally-changed'
    with pytest.raises(BusinessError):app.state.ops.execute(a['id'],u['tenant_id'])
    assert f.calls==[]

def test_shopify_oauth_signature_state_and_replay(product,monkeypatch):
    from urllib.parse import urlparse,parse_qs,urlencode
    import backend.production.api as endpoints
    app,c,f=product;u=register(app,c)
    monkeypatch.setattr(endpoints,'credentials',lambda:{'SHOPIFY_CLIENT_ID':'public-id','SHOPIFY_CLIENT_SECRET':'oauth-secret'})
    monkeypatch.setattr(endpoints,'exchange',lambda d,v:{'access_token':'private-oauth-token','refresh_token':'private-refresh','expires_in':86400})
    monkeypatch.setattr(endpoints,'Shopify',lambda d,t:f)
    authorize=post(c,'/shopify/connect',{'shop':'oauth-test.myshopify.com'}).json()['url']
    nonce=parse_qs(urlparse(authorize).query)['state'][0]
    p={'shop':'oauth-test.myshopify.com','state':nonce,'code':'code','timestamp':str(int(clock()))}
    message='&'.join(k+'='+v for k,v in sorted(p.items()))
    p['hmac']=hmac.new(b'oauth-secret',message.encode(),hashlib.sha256).hexdigest()
    assert c.get('/api/shopify/callback?'+urlencode({**p,'code':'tampered'}),follow_redirects=False).status_code==403
    assert c.get('/api/shopify/callback?'+urlencode(p)+'&code=duplicate',follow_redirects=False).status_code==403
    assert c.get('/api/shopify/callback?'+urlencode(p),follow_redirects=False).status_code==303
    assert c.get('/api/shopify/callback?'+urlencode(p),follow_redirects=False).status_code==403
    assert 'private-oauth-token' not in c.get('/api/state').text

def test_feishu_bound_message_dedup_and_completed_task_notification(product,monkeypatch):
    import backend.production.feishu as transport
    app,c,f=product;u=register(app,c);shop=connect(app,u)
    secret={'app_id':'cli_example','app_secret':'private','verification_token':'verification','encrypt_key':'encryption'};cid=uid('conn');sent=[]
    with app.state.db.transaction() as db:
        db.execute(update(users).where(users.c.id==u['id']).values(feishu_open_id='ou_test'))
        db.execute(connections.insert().values(id=cid,tenant_id=u['tenant_id'],provider='feishu',external_id=secret['app_id'],secret=app.state.vault.seal(secret),status='active',info={},created_at=clock()))
    monkeypatch.setattr(transport,'tenant_token',lambda secret:'private-token')
    monkeypatch.setattr(app.state.feishu,'send',lambda token,oid,typ,content,key:sent.append((oid,content,key)))
    con=app.state.db.get(connections,cid,u['tenant_id'])
    v={'header':{'event_id':'bound-e1','event_type':'im.message.receive_v1'},'event':{'sender':{'sender_type':'user','sender_id':{'open_id':'ou_test'}},'message':{'message_type':'text','content':'{"text":"同步订单"}'}}}
    app.state.feishu.receive(con,v);app.state.feishu.receive(con,v)
    ev=app.state.db.list(events)[0];app.state.feishu.event_job(ev['id'],u['tenant_id'])
    task=app.state.db.list(tasks,u['tenant_id'])[0];app.state.ops.process_task(task['id'],u['tenant_id']);app.state.feishu.notify_task(task['id'],u['tenant_id'])
    assert len(app.state.db.list(events))==1 and len(sent)==2 and sent[1][0]=='ou_test'
    assert '已完成' in sent[1][1]['text']
    with app.state.db.transaction() as db:db.execute(update(users).where(users.c.id==u['id']).values(active=False))
    with pytest.raises(FeishuError):app.state.feishu.notify_task(task['id'],u['tenant_id'])

def test_partial_mutation_response_is_unknown():
    import httpx
    def response(r):return httpx.Response(200,json={'data':{'orderCreate':None},'errors':[{'path':['orderCreate','order'],'extensions':{'code':'ACCESS_DENIED'}}]})
    api=Shopify('test.myshopify.com','token',httpx.MockTransport(response))
    with pytest.raises(ShopifyError) as exc:api.gql('mutation{orderCreate}',mutation=True)
    assert exc.value.unknown

def test_feishu_card_preview_and_result_notify_only_active_participants(product,monkeypatch):
    import backend.production.feishu as transport
    app,c,f=product;u=register(app,c);shop=connect(app,u);v=other_approver(app,u);sent=[]
    secret={'app_id':'cli_preview','app_secret':'private'};cid=uid('conn')
    with app.state.db.transaction() as db:
        db.execute(update(users).where(users.c.id==u['id']).values(feishu_open_id='ou_requester'))
        db.execute(update(users).where(users.c.id==v['id']).values(feishu_open_id='ou_approver'))
        db.execute(connections.insert().values(id=cid,tenant_id=u['tenant_id'],provider='feishu',external_id=secret['app_id'],secret=app.state.vault.seal(secret),status='active',info={},created_at=clock()))
    monkeypatch.setattr(transport,'tenant_token',lambda secret:'private-token')
    monkeypatch.setattr(app.state.feishu,'send',lambda token,oid,typ,content,key:sent.append((oid,typ,content)))
    a=propose(app,u,shop);app.state.feishu.notify(a['id'],u['tenant_id'])
    assert len(sent)==1 and sent[0][0]=='ou_approver'
    preview=sent[0][2]['elements'][0]['text']
    assert preview['tag']=='plain_text' and '10 → 20' in preview['content']
    app.state.ops.decide(v,a['id'],True);app.state.ops.execute(a['id'],u['tenant_id'])
    sent.clear();app.state.feishu.notify(a['id'],u['tenant_id'],True)
    assert {x[0] for x in sent}=={'ou_requester','ou_approver'} and all('已执行' in x[2]['text'] for x in sent)
    with app.state.db.transaction() as db:db.execute(update(users).where(users.c.id==v['id']).values(active=False))
    sent.clear();app.state.feishu.notify(a['id'],u['tenant_id'],True)
    assert [x[0] for x in sent]==['ou_requester']
    v2=other_approver(app,u);a2=propose(app,u,shop);app.state.ops.decide(v2,a2['id'],False)
    assert app.state.db.list(jobs,u['tenant_id'],1,kind='notify_result',target_id=a2['id'])

def test_notification_retry_never_requeues_business_write(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);a=propose(app,u,cid)
    v=other_approver(app,u);app.state.ops.decide(v,a['id'],True)
    with app.state.db.transaction() as db:db.execute(update(jobs).where(jobs.c.tenant_id==u['tenant_id']).values(status='failed'))
    notice=app.state.db.list(jobs,u['tenant_id'],1,kind='notify')[0]
    write=app.state.db.list(jobs,u['tenant_id'],1,kind='action')[0]
    assert post(c,'/jobs/'+notice['id']+'/retry',{}).status_code==200
    assert post(c,'/jobs/'+write['id']+'/retry',{}).status_code==422
    assert app.state.db.get(jobs,write['id'],u['tenant_id'])['status']=='failed' and not f.calls

def test_feishu_encrypted_challenge_matches_official_envelope():
    from Crypto.Cipher import AES
    secret={'app_id':'cli_test','verification_token':'verify-token','encrypt_key':'encryption-key'}
    payload={'type':'url_verification','token':'verify-token','challenge':'challenge-value'}
    plain=json.dumps(payload).encode();pad=16-len(plain)%16;iv=b'x'*16
    cipher=AES.new(hashlib.sha256(b'encryption-key').digest(),AES.MODE_CBC,iv)
    body=json.dumps({'encrypt':base64.b64encode(iv+cipher.encrypt(plain+bytes([pad])*pad)).decode()}).encode()
    assert verify_event(body,{},secret)==payload
    with pytest.raises(FeishuError):verify_event(body,{},dict(secret,verification_token='wrong-token'))

def test_privacy_request_is_reviewed_and_redacted_order_is_not_resynced(product):
    app,c,f=product;u=register(app,c);cid=connect(app,u);con=app.state.db.get(connections,cid,u['tenant_id'])
    sync=app.state.ops.submit(u,'sync','sync',{},cid);app.state.ops.process_task(sync['id'],u['tenant_id'])
    a=propose(app,u,cid,'refund',{'order_id':f.o['id'],'amount':'1.00'})
    body=json.dumps({'customer':{'email':'private@example.com'},'orders_to_redact':[1]}).encode()
    headers={'X-Shopify-Shop-Domain':con['external_id'],'X-Shopify-Topic':'customers/redact','X-Shopify-Event-Id':'privacy1','X-Shopify-Hmac-Sha256':base64.b64encode(hmac.new(b'private',body,hashlib.sha256).digest()).decode()}
    assert c.post('/hooks/shopify',content=body,headers=headers).status_code==200
    ev=app.state.db.list(events)[0];app.state.worker.shopify_event(ev['id'],u['tenant_id'])
    assert 'private@example.com' not in c.get('/api/state').text
    assert c.get('/api/privacy/'+ev['id']+'/export').json()['orders'][0]['id']==f.o['id']
    assert post(c,'/privacy/'+ev['id']+'/resolve',{'note':'data checked','erase_snapshots':True}).status_code==200
    assert app.state.db.get(actions,a['id'],u['tenant_id'])['payload']=={}
    task=app.state.ops.submit(u,'sync again','sync',{},cid);app.state.ops.process_task(task['id'],u['tenant_id'])
    assert app.state.db.list(resources,u['tenant_id'],kind='order')==[]
    with pytest.raises(BusinessError):propose(app,u,cid,'refund',{'order_id':f.o['id'],'amount':'1.00'})
    assert app.state.db.verify(u['tenant_id'])['valid']
    app.state.ops.process_task(a['task_id'],u['tenant_id'])
    assert app.state.db.get(tasks,a['task_id'],u['tenant_id'])['status']=='redacted'

def test_confirmed_knowledge_retrieves_chinese_market_name(product):
    app,c,f=product;u=register(app,c)
    k=post(c,'/knowledge',{'title':'德国能效说明','market':'DE','content':'回答前引用已确认的规格表','evidence':['确认的产品规格 v1']}).json()
    task=app.state.ops.submit(u,'德国客户询问能效如何回复','chat',{'use_ai':False})
    app.state.ops.process_task(task['id'],u['tenant_id'])
    result=app.state.db.get(tasks,task['id'],u['tenant_id'])['result']
    assert result['statistics']['knowledge'][0]['id']==k['id']
    assert result['reports'][2]['evidence']==[k['id']]

@pytest.mark.parametrize('value',['0','-1','0.001','NaN','Infinity',None])
def test_exact_money(value):
    with pytest.raises(BusinessError):cents(value)

@pytest.mark.parametrize('value',['https://shop.myshopify.com','evil.example','shop.myshopify.com/..','shop.myshopify.com@evil.example'])
def test_shopify_domain_ssrf(value):
    with pytest.raises(ValueError):shop_domain(value)

def test_gql_mutation_timeout_never_blindly_retried():
    import httpx
    calls=[]
    def response(r):calls.append(r);raise httpx.ReadTimeout('timeout')
    api=Shopify('test.myshopify.com','token',httpx.MockTransport(response))
    with pytest.raises(ShopifyError) as exc:api.gql('mutation{anything}',mutation=True)
    assert exc.value.unknown and len(calls)==1
