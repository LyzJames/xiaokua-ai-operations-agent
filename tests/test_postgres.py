"""Run against a disposable real PostgreSQL with scripts/run_postgres_tests.py."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from sqlalchemy import select,update
from backend.production.database import Database,tenants,jobs,connections,audits,uid,clock
from backend.production.worker import Worker
from backend.production.security import Settings,Vault
from backend.production.shopify import for_connection

pytestmark=pytest.mark.skipif(not os.getenv('XIAOKUA_TEST_POSTGRES'),reason='PostgreSQL acceptance runtime not selected')
@pytest.fixture
def pg():
    db=Database(os.environ['XIAOKUA_TEST_POSTGRES'])
    t=uid('org')
    with db.transaction() as c:c.execute(tenants.insert().values(id=t,name='PostgreSQL acceptance',audit_head='0'*64,settings={},created_at=clock()))
    yield db,t
    with db.transaction() as c:c.execute(update(jobs).where(jobs.c.tenant_id==t).values(status='done',lease_until=0))
    db.engine.dispose()

def test_postgres_concurrent_audit_chain_and_append_only(pg):
    db,t=pg
    def append(i):
        with db.transaction() as c:db.audit(c,t,'test','concurrent',str(i),{})
    with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(append,range(40)))
    assert db.verify(t)['valid'] and db.verify(t)['count']==40
    with pytest.raises(Exception):
        with db.transaction() as c:c.execute(update(audits).where(audits.c.tenant_id==t).values(event='tamper'))

def test_postgres_workers_claim_each_job_once(pg):
    db,t=pg
    with db.transaction() as c:
        for i in range(40):db.enqueue(c,t,'test',uid('target'))
    def claim(_):
        w=Worker(db,None,None)
        return w.claim()['id']
    with ThreadPoolExecutor(max_workers=8) as pool:ids=list(pool.map(claim,range(40)))
    assert len(set(ids))==40

def test_postgres_token_refresh_is_serialized(pg,tmp_path,monkeypatch):
    from backend.production import shopify
    db,t=pg;vault=Vault(Settings(tmp_path,'sqlite:///'+str(tmp_path/'unused.sqlite')));cid=uid('conn')
    secret={'client_id':'id','client_secret':'secret','access_token':'expired','refresh_token':'refresh','expires_at':clock()-1}
    with db.transaction() as c:c.execute(connections.insert().values(id=cid,tenant_id=t,provider='shopify',external_id=uid('shop').replace('_','-')+'.myshopify.com',secret=vault.seal(secret),status='active',info={},created_at=clock()))
    calls=[]
    def exchange(domain,values):
        calls.append(values);return {'access_token':'new','refresh_token':'new-refresh','expires_in':3600}
    monkeypatch.setattr(shopify,'exchange',exchange)
    with ThreadPoolExecutor(max_workers=4) as pool:tokens=list(pool.map(lambda _:for_connection(db,vault,cid,t).token,range(4)))
    assert tokens==['new']*4 and len(calls)==1

def test_postgres_durable_job_recovery(pg):
    db,t=pg
    with db.transaction() as c:
        db.enqueue(c,t,'test',uid('target'))
    w=Worker(db,None,None);claimed=w.claim()
    with db.transaction() as c:c.execute(update(jobs).where(jobs.c.id==claimed['id']).values(lease_until=clock()-1))
    w.recover()
    assert db.get(jobs,claimed['id'],t)['status']=='queued'


def test_postgres_parallel_role_journal_survives_database_reopen(pg):
    from backend.production.workflows import Operations
    from backend.production.analysis import AnalysisRunner
    from backend.production.database import analysis_steps
    db,t=pg
    task=Operations(db,None,None).submit({'id':'synthetic-pg-operator','tenant_id':t},
        '分析合成订单与库存','analyze',{'use_ai':True})
    calls=[]
    def model(system,context,evidence):
        calls.append(context)
        return {'finding':'当前没有同步业务数据，不能判断真实情况。','evidence':[],
            'limitations':['无同步快照。']},{'attempts':1,'input_tokens':40,'output_tokens':20,'reused':False}
    first=AnalysisRunner(db,model).run(task)
    assert len(calls)==3 and first['analysis']['completed_roles']==3
    reopened=Database(db.url)
    try:
        second=AnalysisRunner(reopened,model).run(task)
        assert len(calls)==3 and second['analysis']['reused_roles']==3
        assert len(reopened.list(analysis_steps,t,task_id=task['id']))==3
        assert reopened.verify(t)['valid']
    finally:reopened.engine.dispose()


def test_postgres_shared_pool_parallel_observations_do_not_double_reserve_or_ship(pg,tmp_path):
    from backend.demo_shop import DemoShop
    from backend.production.workflows import Operations
    from backend.production.shopify import upsert_resource
    from backend.production.database import inventory_pools, inventory_positions, stock_journal
    db,t=pg
    settings=Settings(tmp_path,'sqlite:///'+str(tmp_path/'unused.sqlite'),worker_enabled=False)
    shops={uid('conn'):DemoShop(tmp_path/f'store-{i}.json') for i in range(2)}
    ops=Operations(db,None,settings,lambda cid,tenant:shops[cid])
    actor={'id':'synthetic-stock-owner','tenant_id':t}
    with db.transaction() as c:
        for cid,shop in shops.items():
            c.execute(connections.insert().values(id=cid,tenant_id=t,provider='shopify',external_id=cid+'.myshopify.com',secret='',status='active',
                info={'demo':True,'last_sync':clock(),'order_access':True,'sync_errors':[]},created_at=clock()))
            for value in shop.orders():upsert_resource(c,t,cid,'order',value)
            for value in shop.variants():upsert_resource(c,t,cid,'variant',value)
    pool=ops.shared.create_pool(actor,'Synthetic shared stock','DEMO-BULB-01','synthetic-eu')
    for cid in shops:ops.shared.add_mapping(actor,pool['id'],cid,'gid://shopify/ProductVariant/1','gid://shopify/Location/1')
    with db.transaction() as c:c.execute(update(inventory_pools).where(inventory_pools.c.id==pool['id']).values(on_hand=100,initialized=True))
    identifiers=list(shops)
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda i:ops.shared.ingest(identifiers[i%2],t),range(12)))
    state=ops.shared.state(t)['pools'][0]
    assert (state['on_hand'],state['reserved'],state['available'])==(100,4,91)
    assert len(db.list(inventory_positions,t))==4
    cid=identifiers[0];shop=shops[cid]
    shop.fulfill(shop.order('gid://shopify/Order/1'),{'company':'Synthetic','number':'TEST-PG'})
    with db.transaction() as c:
        for value in shop.orders():upsert_resource(c,t,cid,'order',value)
    with ThreadPoolExecutor(max_workers=6) as executor:
        list(executor.map(lambda _:ops.shared.ingest(cid,t),range(12)))
    state=ops.shared.state(t)['pools'][0]
    assert (state['on_hand'],state['reserved'])==(99,3)
    assert sum(j['delta'] for j in db.list(stock_journal,t,100) if j['kind']=='order_observed')==-1
    assert db.verify(t)['valid']
    with pytest.raises(Exception):
        with db.transaction() as c:c.execute(update(stock_journal).where(stock_journal.c.tenant_id==t).values(delta=9))
