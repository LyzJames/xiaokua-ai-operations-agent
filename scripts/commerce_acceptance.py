"""Own-account multi-platform acceptance. Amazon writes target its dynamic sandbox only.

Every virtual write passes through the application task, independent approval,
durable worker and readback paths. Synthetic reviewer identities are test fixtures;
this does not replace the human approval acceptance performed in Feishu/Web.
Raw runtime data stays under ignored data/. Public reports contain status only.
"""
import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi.testclient import TestClient
from sqlalchemy import update

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.production.api import create_app
from backend.production.security import Settings, password_hash
from backend.production.database import users, actions, tasks, connections, jobs, clock, uid
from backend.production.platforms.contracts import PlatformError


def run(model=False, shopify_write=False):
    directory=ROOT/'data'/('commerce-live-'+uuid.uuid4().hex[:12])
    app=create_app(Settings(directory,'sqlite:///'+str(directory/'product.sqlite'),public_url='http://testserver',worker_enabled=False,test_only=True))
    checks=[]
    def record(name, passed=True, **extra):
        checks.append({'name':name,'passed':passed,**extra})
        print(json.dumps(checks[-1],ensure_ascii=False),flush=True)
    with TestClient(app) as client:
        def post(path, body):
            csrf=client.get('/api/bootstrap').json()['csrf']
            result=client.post('/api'+path,json=body,headers={'X-CSRF-Token':csrf})
            if result.status_code not in (200,201):
                # No raw HTTP bodies: provider/account identifiers remain private.
                raise RuntimeError('APPLICATION_HTTP_'+str(result.status_code))
            return result.json()
        account=post('/auth/register',{'email':'operator@example.invalid','password':'synthetic-live-operator-123',
            'name':'Synthetic acceptance operator','organization':'Own-account platform acceptance',
            'setup_token':(directory/'setup.token').read_text()})['user']
        approver={'id':uid('test-reviewer'),'tenant_id':account['tenant_id'],'email':'reviewer@example.invalid','name':'Synthetic acceptance reviewer',
            'role':'approver','active':True,'password':password_hash('synthetic-reviewer-123'),'feishu_open_id':None,'created_at':clock()}
        with app.state.db.transaction() as c:c.execute(users.insert().values(**approver))
        amazon=post('/amazon/import-local',{})['id']
        record('Amazon LWA + FBA sandbox connection',environment='amazon_dynamic_sandbox')
        shop=post('/shopify/import-local',{})['id']
        record('Shopify installed development-store connection')
        def work():
            for _ in range(120):
                with app.state.db.transaction() as c:c.execute(update(jobs).where(jobs.c.status=='queued').values(available_at=0))
                if not app.state.worker.run_once():return
            raise RuntimeError('JOBS_DID_NOT_SETTLE')
        parent=post('/commands',{'command':'同步并分析所选店铺','connection_ids':[shop,amazon],'idempotency_key':'live-scope-'+uuid.uuid4().hex,'use_ai':model})
        work()
        result=app.state.db.get(tasks,parent['id'],account['tenant_id'])
        children=[app.state.db.get(tasks,v['id'],account['tenant_id']) for v in result['result'].get('children',[])]
        record('Web multi-platform sync and analysis chain',result['status']=='completed' and len(children)==4,
            mode='own-account APIs + model' if model else 'own-account APIs + deterministic analysis',
            shopify_orders='live test-store records',amazon_orders='hosted regional static fixture',amazon_inventory='dynamic sandbox')
        if model:
            reports=[r for child in children for r in child['result'].get('reports',[])]
            record('DeepSeek role reports validated',len(reports)==6 and all(r['mode']=='deepseek' for r in reports),roles=len(reports))
        def approved(kind, payload, cid=amazon):
            task=post('/tasks',{'command':'Own-account sandbox acceptance '+kind,'kind':kind,'params':payload,'connection_id':cid,'idempotency_key':uid('live-case')})
            work()
            proposed=app.state.db.list(actions,account['tenant_id'],1,task_id=task['id'])
            if not proposed:raise RuntimeError('PROPOSAL_NOT_CREATED_'+kind)
            action=proposed[0]
            app.state.ops.decide(approver,action['id'],True,'Synthetic independent test-reviewer approval; zero real money')
            work()
            current=app.state.db.get(actions,action['id'],account['tenant_id'])
            record('Approved '+kind+' with readback',current['status']=='executed',environment='shopify_test_store' if cid==shop else 'amazon_dynamic_sandbox')
            return current
        sku='xiaokua-test-'+uuid.uuid4().hex[:12]
        approved('sandbox_sku',{'sku':sku})
        approved('sandbox_stock',{'sku':sku,'quantity':10})
        first=approved('mcf_create',{'sku':sku,'quantity':2})
        if first['status']!='executed':raise RuntimeError('MCF_CREATE_REQUIRES_REVIEW')
        completed=approved('sandbox_status',{'order_id':first['result']['order_id'],'status':'COMPLETE'})
        for package in completed['result'].get('packages',[]):
            current=package['status']
            for status in ('OUT_FOR_DELIVERY','DELIVERED'):
                approved('sandbox_package',{'order_id':first['result']['order_id'],'package_id':package['package_id'],'status':status})
        second=approved('mcf_create',{'sku':sku,'quantity':1})
        if second['status']!='executed':raise RuntimeError('MCF_CREATE_REQUIRES_REVIEW')
        approved('mcf_cancel',{'order_id':second['result']['order_id']})
        final_stock=app.state.ops.client(amazon,account['tenant_id']).inventory(sku)
        record('FBA availability after virtual completion and cancellation',final_stock['quantity']==8,expected_quantity=8,observed_quantity=final_stock['quantity'],observed_reserved=final_stock['reserved'])
        if shopify_write:
            snapshots=client.get('/api/state').json()['variants']
            candidate=next((v for v in snapshots if v['connection_id']==shop and 'xiaokua-test' in v['data'].get('product',{}).get('tags',[]) and v['normalized']['locations_complete'] and v['normalized']['locations']),None)
            if not candidate:raise RuntimeError('NO_SHOPIFY_TAGGED_TEST_VARIANT')
            native=candidate['normalized'];loc=native['locations'][0]
            current=approved('inventory',{'item_id':native['inventory_item_id'],'location_id':loc['id'],'quantity':max(0,loc['available'])},shop)
            record('Shopify compare-and-set validation with unchanged target',current['status']=='executed',net_inventory_change=0)
        record('Tenant audit chain verified',app.state.db.verify(account['tenant_id'])['valid'])
        record('No secret or buyer identity in exported state',all(v not in client.get('/api/state').text for v in ('buyerEmail','deliveryAddress','client_secret','refresh_token')))
    return {'schema':1,'captured_at':datetime.now(timezone.utc).isoformat(),'scope':'Own-account hosted sandbox acceptance, not production Amazon fulfillment',
        'approval':'two distinct synthetic fixture identities; separate Web/Feishu human approval evidence in test report',
        'payments':0,'production_amazon_writes':0,'checks':checks,'passed':all(c['passed'] for c in checks)}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-sandbox',action='store_true',help='Explicit opt-in to virtual Amazon sandbox writes using your own credentials')
    parser.add_argument('--model',action='store_true',help='Use your configured model API for read-only analysis')
    parser.add_argument('--shopify-test-write',action='store_true',help='Test a tagged dev-store inventory CAS with the same target quantity')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if not args.live_sandbox:parser.error('--live-sandbox is required; this command contacts your own platform accounts')
    try:report=run(args.model,args.shopify_test_write)
    except Exception as error:
        report={'schema':1,'passed':False,'code':error.code if isinstance(error,PlatformError) else str(error) if isinstance(error,RuntimeError) else type(error).__name__,
            'captured_at':datetime.now(timezone.utc).isoformat(),'note':'Raw runtime remains under ignored data/; no write is automatically resent.'}
        print(json.dumps(report,ensure_ascii=False),flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    raise SystemExit(0 if report['passed'] else 1)
