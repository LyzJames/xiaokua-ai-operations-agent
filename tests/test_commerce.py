"""Complete multi-shop command and shared-stock cases with isolated synthetic merchants."""
import copy
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from backend.demo import create_demo, DEMO_ORG, SHOP_CONNECTION, DEMO_PASSWORD
from backend.production.database import connections, resources, users, tasks, actions, jobs, inventory_pools, stock_plans, stock_journal, clock
from backend.production.commands import submit_command
from backend.production.workflows import BusinessError
from backend.production.platforms.contracts import normalize_resource, PlatformError, CommerceAdapter
from backend.production.platforms.registry import connection_adapter

B = "conn_demo_shopify_b"


def post(client, path, body):
    token = client.get('/api/bootstrap').json()['csrf']
    return client.post('/api'+path, json=body, headers={'X-CSRF-Token':token})


def login(client, role):
    result = post(client, '/auth/login', {'email':role+'@example.invalid','password':DEMO_PASSWORD})
    assert result.status_code == 200, result.text


def settle(app):
    # Advance queued timers rather than sleeping. External writes are never retried.
    for _ in range(100):
        with app.state.db.transaction() as c:
            c.execute(update(jobs).where(jobs.c.status=='queued').values(available_at=0))
        if not app.state.worker.run_once():
            return
    raise AssertionError('jobs did not settle')


@pytest.fixture
def commerce(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('synthetic case attempted external HTTP')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', deny)
    app = create_demo(tmp_path, worker_enabled=False, multi_store=True)
    with TestClient(app) as client:
        login(client,'owner')
        actor = app.state.db.get(users,'user_demo_owner',DEMO_ORG)
        approver = app.state.db.get(users,'user_demo_approver',DEMO_ORG)
        for cid in (SHOP_CONNECTION,B):
            t = app.state.ops.submit(actor,'同步','sync',{},cid)
            app.state.ops.process_task(t['id'],DEMO_ORG)
        yield app, client, actor, approver


def initialize(app, actor, approver, quantity=40):
    pool = app.state.ops.shared.create_pool(actor,'灯泡共享仓','DEMO-BULB-01','merchant-eu')
    # Map before opening the physical ledger so existing shipments form the opening balance.
    mappings = [app.state.ops.shared.add_mapping(actor,pool['id'],cid,'gid://shopify/ProductVariant/1','gid://shopify/Location/1') for cid in (SHOP_CONNECTION,B)]
    task = app.state.ops.submit(actor,'初始盘点','pool_adjust',{'pool_id':pool['id'],'quantity':quantity})
    app.state.ops.process_task(task['id'],DEMO_ORG)
    action = app.state.db.list(actions,DEMO_ORG,1,task_id=task['id'])[0]
    app.state.ops.decide(approver,action['id'],True,'盘点已核实')
    app.state.ops.execute(action['id'],DEMO_ORG)
    settle(app)
    return pool['id'], mappings


def pool_state(app, identifier):
    return next(p for p in app.state.ops.shared.state(DEMO_ORG)['pools'] if p['id']==identifier)


@pytest.mark.business
def test_web_command_complete_two_stores_separate_approvals_and_readback(commerce):
    app,c,actor,approver = commerce
    body={'command':'将 SKU DEMO-BULB-01 库存设为 8','connection_ids':[SHOP_CONNECTION,B],'use_ai':False,'idempotency_key':'two-shop-command'}
    parent=post(c,'/commands',body)
    assert parent.status_code==201, parent.text
    assert post(c,'/commands',body).json()['id']==parent.json()['id']
    settle(app)
    requested=app.state.db.list(actions,DEMO_ORG)
    assert len(requested)==2 and {a['connection_id'] for a in requested}=={SHOP_CONNECTION,B}
    assert all(shop.data['quantity']==10 for shop in app.state.demo_shops.values())
    assert post(c,'/approvals/'+requested[0]['id'],{'approved':True}).status_code==422
    app.state.ops.decide(approver,requested[0]['id'],True,'单店审批')
    settle(app)
    assert sorted(s.data['quantity'] for s in app.state.demo_shops.values())==[8,10]
    assert app.state.db.get(tasks,parent.json()['id'],DEMO_ORG)['status']=='awaiting_approval'
    app.state.ops.decide(approver,requested[1]['id'],True,'第二家店审批')
    settle(app)
    assert app.state.db.get(tasks,parent.json()['id'],DEMO_ORG)['status']=='completed'
    assert all(s.data['quantity']==8 and len(s.data['writes'])==1 for s in app.state.demo_shops.values())
    assert len(c.get('/api/state').json()['variants'])==2
    assert '店铺子任务' in c.get('/api/tasks/'+parent.json()['id']+'/report').text
    assert app.state.db.verify(DEMO_ORG)['valid']


@pytest.mark.agent
def test_feishu_and_web_scope_ambiguity_and_compound_dag_offline(commerce):
    app,c,actor,_=commerce
    assert post(c,'/demo/events',{'text':'同步店铺'}).status_code==200
    settle(app)
    assert not app.state.db.list(tasks,DEMO_ORG,100,kind='command')
    assert post(c,'/demo/events',{'text':'同步并分析所有店铺'}).status_code==200
    settle(app)
    parent=app.state.db.list(tasks,DEMO_ORG,1,kind='command')[0]
    assert parent['status']=='completed' and len(parent['result']['children'])==4
    assert parent['params']['entrypoint']=='feishu' and parent['params']['use_ai'] is False
    assert all(app.state.db.get(tasks,child['id'],DEMO_ORG)['status']=='completed' for child in parent['result']['children'])
    for cid in (SHOP_CONNECTION,B):
        pair=[app.state.db.get(tasks,child['id'],DEMO_ORG) for child in parent['result']['children'] if child['connection_id']==cid]
        assert pair[1]['params']['depends_on']==[pair[0]['id']]


@pytest.mark.security
def test_command_cannot_forge_scope_internal_params_or_model_numbers(commerce,monkeypatch):
    app,c,actor,_=commerce
    assert post(c,'/commands',{'command':'同步','connection_ids':[SHOP_CONNECTION,'foreign'],'idempotency_key':'foreign'}).status_code==422
    assert post(c,'/commands',{'command':'同步','connection_ids':[SHOP_CONNECTION,SHOP_CONNECTION],'idempotency_key':'duplicate'}).status_code==422
    assert post(c,'/tasks',{'command':'库存','kind':'inventory','connection_id':SHOP_CONNECTION,'params':{'_stock_plan':'forged'}}).status_code==422
    assert post(c,'/tasks',{'command':'库存','kind':'inventory','connection_id':SHOP_CONNECTION,'params':{'client_secret':'unallowed','quantity':1}}).status_code==422
    from backend.production import commands
    monkeypatch.setattr(commands,'model_plan',lambda *a:commands.Plan(steps=[commands.Step(kind='inventory',sku='DEMO-BULB-01',quantity=777)]))
    parent=submit_command(app.state.ops,actor,'申请调整商品库存',[SHOP_CONNECTION],'web','model-guess',use_ai=True)
    app.state.ops.process_task(parent['id'],DEMO_ORG)
    assert app.state.db.get(tasks,parent['id'],DEMO_ORG)['status']=='needs_input'
    assert not app.state.db.list(actions,DEMO_ORG)


@pytest.mark.business
def test_shared_inventory_reserve_ship_refund_cancel_and_duplicate_concurrency(commerce):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    assert (pool_state(app,pool)['on_hand'],pool_state(app,pool)['reserved'],pool_state(app,pool)['available'])==(40,4,31)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _:app.state.ops.shared.ingest(SHOP_CONNECTION,DEMO_ORG),range(8)))
    assert pool_state(app,pool)['reserved']==4
    shop=app.state.demo_shops[SHOP_CONNECTION]
    shop.fulfill(shop.order('gid://shopify/Order/1'),{'number':'SYNTHETIC-TRACK','company':'Synthetic Carrier'})
    app.state.ops.sync({'tenant_id':DEMO_ORG,'connection_id':SHOP_CONNECTION,'params':{}})
    assert (pool_state(app,pool)['on_hand'],pool_state(app,pool)['reserved'])==(39,3)
    shop.refund('gid://shopify/Order/1','1.00','USD','gid://shopify/OrderTransaction/1','demo','synthetic-refund')
    app.state.ops.sync({'tenant_id':DEMO_ORG,'connection_id':SHOP_CONNECTION,'params':{}})
    assert pool_state(app,pool)['on_hand']==39
    shop.cancel('gid://shopify/Order/2','CUSTOMER')
    app.state.ops.sync({'tenant_id':DEMO_ORG,'connection_id':SHOP_CONNECTION,'params':{}})
    assert (pool_state(app,pool)['on_hand'],pool_state(app,pool)['reserved'],pool_state(app,pool)['available'])==(39,2,32)
    entries=app.state.db.list(stock_journal,DEMO_ORG,100)
    assert sum(j['delta'] for j in entries if j['kind']=='order_observed')==-1
    with pytest.raises(Exception):
        with app.state.db.transaction() as db:db.execute(update(stock_journal).where(stock_journal.c.pool_id==pool).values(delta=999))
    assert app.state.db.verify(DEMO_ORG)['valid']


@pytest.mark.business
def test_shared_allocation_decreases_verified_before_increase(commerce):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    plan=app.state.ops.shared.create_plan(actor,pool,{mappings[0]['id']:14,mappings[1]['id']:6})
    settle(app)
    assert [s['phase'] for s in plan['steps']]==['decrease','increase']
    current=app.state.db.list(actions,DEMO_ORG,100,status='pending')
    assert len(current)==1 and current[0]['connection_id']==B
    app.state.ops.decide(approver,current[0]['id'],True,'先减库存')
    settle(app)
    assert app.state.demo_shops[B].data['quantity']==6
    pending=app.state.db.list(actions,DEMO_ORG,100,status='pending')
    assert len(pending)==1 and pending[0]['connection_id']==SHOP_CONNECTION
    app.state.ops.decide(approver,pending[0]['id'],True,'再增库存')
    settle(app)
    assert app.state.demo_shops[SHOP_CONNECTION].data['quantity']==14
    assert app.state.db.get(stock_plans,plan['id'],DEMO_ORG)['status']=='completed'
    assert sum(m['quota'] for m in app.state.ops.shared.state(DEMO_ORG)['mappings'])==20
    assert app.state.db.verify(DEMO_ORG)['valid']


@pytest.mark.security
def test_allocation_unknown_decrease_freezes_increases_and_reconcile_never_rewrites(commerce,monkeypatch):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    plan=app.state.ops.shared.create_plan(actor,pool,{mappings[0]['id']:14,mappings[1]['id']:6})
    settle(app)
    shop=app.state.demo_shops[B]
    original=shop.inventory_set
    def unknown(*args):
        original(*args)
        raise PlatformError('SIMULATED_RESPONSE_LOSS',True)
    monkeypatch.setattr(shop,'inventory_set',unknown)
    action=app.state.db.list(actions,DEMO_ORG,1,status='pending')[0]
    app.state.ops.decide(approver,action['id'],True,'确认减少')
    settle(app)
    assert app.state.db.get(actions,action['id'],DEMO_ORG)['status']=='uncertain'
    assert pool_state(app,pool)['frozen']
    assert app.state.demo_shops[SHOP_CONNECTION].data['quantity']==10
    assert not app.state.db.list(actions,DEMO_ORG,100,status='pending')
    with pytest.raises(BusinessError):app.state.ops.execute(action['id'],DEMO_ORG)
    app.state.ops.reconcile(approver,action['id'],'只读回查吻合')
    assert len(shop.data['writes'])==1
    assert app.state.db.get(stock_plans,plan['id'],DEMO_ORG)['status']=='needs_review'


@pytest.mark.security
def test_stock_bounds_stale_state_incomplete_orders_and_disconnect(commerce):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    with pytest.raises(BusinessError):app.state.ops.shared.create_plan(actor,pool,{m['id']:100 for m in mappings})
    with pytest.raises(BusinessError):app.state.ops.shared.create_plan(actor,pool,{mappings[0]['id']:1})
    task=app.state.ops.submit(actor,'绕过共享池','inventory',{'item_id':'gid://shopify/InventoryItem/1','location_id':'gid://shopify/Location/1','quantity':100},SHOP_CONNECTION)
    with pytest.raises(BusinessError):app.state.ops.process_task(task['id'],DEMO_ORG)
    shop=app.state.demo_shops[SHOP_CONNECTION]
    shop.data['orders'][0]['lineItems']['pageInfo']['hasNextPage']=True
    app.state.ops.sync({'tenant_id':DEMO_ORG,'connection_id':SHOP_CONNECTION,'params':{}})
    assert pool_state(app,pool)['frozen']
    with pytest.raises(BusinessError):app.state.ops.shared.clear_freeze(actor,pool,'不能忽略明细缺失')
    assert post(c,'/connections/'+B+'/disconnect',{}).status_code==200
    assert 'CONNECTION_REVOKED' in pool_state(app,pool)['issues']
    assert app.state.db.verify(DEMO_ORG)['valid']


@pytest.mark.security
def test_stock_approval_expiry_and_rejection_freeze_plan(commerce):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    plan=app.state.ops.shared.create_plan(actor,pool,{mappings[0]['id']:14,mappings[1]['id']:6})
    settle(app)
    action=app.state.db.list(actions,DEMO_ORG,1,status='pending')[0]
    with app.state.db.transaction() as db:db.execute(update(actions).where(actions.c.id==action['id']).values(expires_at=clock()-1))
    app.state.worker.recover();settle(app)
    assert app.state.db.get(stock_plans,plan['id'],DEMO_ORG)['status']=='needs_review'
    assert pool_state(app,pool)['frozen']
    assert not app.state.demo_shops[SHOP_CONNECTION].data['writes']


@pytest.mark.business
@pytest.mark.parametrize('provider',['taobao','jd','pinduoduo','temu'])
def test_unqualified_provider_requires_adapter_contract_not_implicit_access(commerce,provider):
    app,c,actor,_=commerce
    identifier='synthetic-'+provider
    with app.state.db.transaction() as db:
        db.execute(connections.insert().values(id=identifier,tenant_id=DEMO_ORG,provider=provider,external_id=identifier,secret='',status='active',info={},created_at=clock()))
    with pytest.raises(PlatformError,match='PROVIDER_NOT_INTEGRATED'):
        connection_adapter(app.state.db,app.state.vault,identifier,DEMO_ORG)
    state=c.get('/api/state').json()
    con=next(v for v in state['connections'] if v['id']==identifier)
    assert con['capabilities']['operations']==[] and con['capabilities']['qualification_required']


@pytest.mark.business
@pytest.mark.parametrize('provider',['taobao','jd','pinduoduo','temu'])
def test_future_platform_adapter_runs_same_action_and_inventory_contract(commerce,provider):
    app,c,actor,approver=commerce
    identifier='contract-'+provider
    snapshot=copy.deepcopy(c.get('/api/state').json()['variants'][0]['normalized'])
    snapshot.update(provider=provider,id=provider+':variant',product_id=provider+':product',inventory_item_id=provider+':stock',environment='test',source='Synthetic adapter contract only')
    snapshot['locations']=[{'id':provider+':warehouse','name':'Synthetic warehouse','available':10,'pool_type':'merchant'}]
    class ContractAdapter:
        def __init__(self):self.provider=provider;self.quantity=10;self.writes=0
        def capabilities(self):return {'provider':provider,'operations':['sync','inventory','analyze','chat'],'environment':'test'}
        def snapshot_orders(self,params):return []
        def snapshot_products(self):
            value=copy.deepcopy(snapshot);value['available']=self.quantity;value['locations'][0]['available']=self.quantity;return [value]
        def inventory_position(self,item_id,location_id):return {'item_id':item_id,'location_id':location_id,'quantity':self.quantity,'pool_type':'merchant'}
        def prepare_action(self,kind,p,test_only):return {'quantity':self.quantity},dict(p)
        def execute_action(self,kind,p,before,key,test_only):
            assert before['quantity']==self.quantity;self.quantity=p['quantity'];self.writes+=1;return {'verified_quantity':self.quantity}
        def verify_action(self,kind,p):return self.quantity==p['quantity'],{'quantity':self.quantity}
    api=ContractAdapter();assert isinstance(api,CommerceAdapter)
    original_factory=app.state.ops.client_factory
    app.state.ops.client_factory=lambda cid,t:api if cid==identifier else original_factory(cid,t)
    with app.state.db.transaction() as db:
        db.execute(connections.insert().values(id=identifier,tenant_id=DEMO_ORG,provider=provider,external_id=identifier,secret='',status='active',info={'contract_test':True},created_at=clock()))
    sync=app.state.ops.submit(actor,'契约同步','sync',{},identifier);app.state.ops.process_task(sync['id'],DEMO_ORG)
    resource=app.state.db.list(resources,DEMO_ORG,1,connection_id=identifier,kind='variant')[0]
    assert normalize_resource(provider,'variant',resource['data'])['provider']==provider
    task=app.state.ops.submit(actor,'契约写入','inventory',{'item_id':snapshot['inventory_item_id'],'location_id':snapshot['locations'][0]['id'],'quantity':7},identifier)
    app.state.ops.process_task(task['id'],DEMO_ORG)
    action=app.state.db.list(actions,DEMO_ORG,1,task_id=task['id'])[0]
    app.state.ops.decide(approver,action['id'],True,'测试契约，不宣称平台联调')
    app.state.ops.execute(action['id'],DEMO_ORG)
    app.state.ops.execute(action['id'],DEMO_ORG)
    assert api.writes==1 and api.quantity==7
    assert app.state.ops.client(identifier,DEMO_ORG).verify_action('inventory',action['payload'])[0]
    assert app.state.db.verify(DEMO_ORG)['valid']


@pytest.mark.security
def test_shopify_capabilities_follow_granted_write_scopes(commerce):
    from backend.production.platforms.registry import connection_capabilities
    app,c,actor,_=commerce
    con=app.state.db.get(connections,SHOP_CONNECTION,DEMO_ORG)
    con['info']={'scopes':['read_orders','read_inventory','read_products']}
    assert set(connection_capabilities(con)['operations'])=={'sync','analyze','chat'}
    from backend.production.platforms.shopify import ShopifyAdapter
    api=ShopifyAdapter(app.state.demo_shop,con)
    assert set(api.capabilities()['operations'])=={'sync','analyze','chat'}


@pytest.mark.security
def test_allocation_write_acknowledgement_without_readback_stops_increment(commerce,monkeypatch):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    app.state.ops.shared.create_plan(actor,pool,{mappings[0]['id']:14,mappings[1]['id']:6})
    settle(app)
    shop=app.state.demo_shops[B]
    monkeypatch.setattr(shop,'inventory_set',lambda *a:{'inventoryAdjustmentGroup':{'changes':[]},'userErrors':[]})
    action=app.state.db.list(actions,DEMO_ORG,1,status='pending')[0]
    app.state.ops.decide(approver,action['id'],True,'测试平台未生效')
    settle(app)
    assert app.state.db.get(actions,action['id'],DEMO_ORG)['status']=='uncertain'
    assert pool_state(app,pool)['frozen']
    assert app.state.demo_shops[SHOP_CONNECTION].data['quantity']==10
    assert not app.state.db.list(actions,DEMO_ORG,100,status='pending')


@pytest.mark.business
def test_fba_pool_is_read_only_separate_and_needs_no_static_order_reservations(commerce):
    app,c,actor,approver=commerce
    con='synthetic-fba'
    snapshot=copy.deepcopy(c.get('/api/state').json()['variants'][0]['normalized'])
    snapshot.update(provider='amazon',id='amazon:sku:TEST-FBA',product_id='TEST-FBA',inventory_item_id='amazon:sku:TEST-FBA',
        sku='TEST-FBA',available=50,environment='sandbox_dynamic',source='Synthetic FBA inventory contract')
    snapshot['locations']=[{'id':'amazon:fba:ATVPDKIKX0DER','name':'Synthetic FBA','available':50,'pool_type':'platform'}]
    from backend.production.shopify import upsert_resource
    with app.state.db.transaction() as db:
        db.execute(connections.insert().values(id=con,tenant_id=DEMO_ORG,provider='amazon',external_id=con,secret='',status='active',
            info={'environment':'sandbox','last_sync':clock(),'order_access':False,'sync_errors':[{'component':'orders','code':'ORDERS_UNAVAILABLE'}]},created_at=clock()))
        upsert_resource(db,DEMO_ORG,con,'variant',snapshot)
    merchant=app.state.ops.shared.create_pool(actor,'Merchant FBA conflict','TEST-FBA','merchant-conflict')
    with pytest.raises(BusinessError,match='分别建池'):
        app.state.ops.shared.add_mapping(actor,merchant['id'],con,snapshot['id'],snapshot['locations'][0]['id'])
    pool=app.state.ops.shared.create_pool(actor,'FBA independent','TEST-FBA','amazon-fba',pool_type='platform')
    app.state.ops.shared.add_mapping(actor,pool['id'],con,snapshot['id'],snapshot['locations'][0]['id'])
    state=pool_state(app,pool['id'])
    assert (state['on_hand'],state['reserved'],state['available'])==(50,0,45)
    assert not state['frozen']
    with pytest.raises(BusinessError,match='平台维护'):
        app.state.ops.shared.prepare_adjustment(DEMO_ORG,{'pool_id':pool['id'],'quantity':100})


@pytest.mark.security
@pytest.mark.parametrize('missing',['fulfillment_channel','created_at','updated_at'])
def test_unknown_order_metadata_cannot_silently_allow_shared_increases(commerce,monkeypatch,missing):
    app,c,actor,approver=commerce
    pool,mappings=initialize(app,actor,approver)
    from backend.production import inventory
    original=inventory.normalize_resource
    def unknown_channel(provider,kind,value,*args):
        snapshot=original(provider,kind,value,*args)
        if kind=='order':snapshot[missing]='unknown' if missing=='fulfillment_channel' else None
        return snapshot
    monkeypatch.setattr(inventory,'normalize_resource',unknown_channel)
    app.state.ops.shared.ingest(SHOP_CONNECTION,DEMO_ORG)
    assert ('FULFILLMENT_CHANNEL_UNKNOWN' if missing=='fulfillment_channel' else 'ORDER_TIME_UNKNOWN') in pool_state(app,pool)['issues']
    with pytest.raises(BusinessError,match='冻结增库存'):
        app.state.ops.shared.create_plan(actor,pool,{mappings[0]['id']:14,mappings[1]['id']:6})
    with pytest.raises(BusinessError,match='履约渠道' if missing=='fulfillment_channel' else '时间'):
        app.state.ops.shared.clear_freeze(actor,pool,'渠道未知仍不能直接放行')


@pytest.mark.reliability
def test_mapping_uses_native_token_transaction_before_stock_lock(commerce):
    app,c,actor,_=commerce
    from backend.production.shopify import for_connection
    with app.state.db.transaction() as db:
        db.execute(update(connections).where(connections.c.id==SHOP_CONNECTION).values(secret=app.state.vault.seal({'access_token':'synthetic-token','client_secret':'synthetic-client-secret'})))
    token_reads=[]
    original=app.state.ops.client_factory
    def loader(cid,tenant):
        native=for_connection(app.state.db,app.state.vault,cid,tenant)
        token_reads.append(native.token)
        return original(cid,tenant)
    app.state.ops.client_factory=loader
    pool=app.state.ops.shared.create_pool(actor,'Credential transaction fixture','DEMO-BULB-01','credential-test-warehouse')
    mapping=app.state.ops.shared.add_mapping(actor,pool['id'],SHOP_CONNECTION,'gid://shopify/ProductVariant/1','gid://shopify/Location/1')
    assert mapping['quota']==10 and token_reads==['synthetic-token']
    assert app.state.db.verify(DEMO_ORG)['valid']
