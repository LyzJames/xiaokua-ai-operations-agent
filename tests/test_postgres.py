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
