"""Analysis isolation, native parallelism, restart/reuse and retrieval invalidation."""
import threading
import pytest
from sqlalchemy import update
from backend.production.database import Database, tenants, resources, tasks, knowledge, analysis_steps, clock
from backend.production.workflows import Operations
from backend.production.analysis import AnalysisRunner
from backend.production import analysis
from backend.model_runtime import ModelFailure

pytestmark = pytest.mark.agent


@pytest.fixture
def environment(tmp_path):
    db = Database('sqlite:///' + str(tmp_path / 'analysis.sqlite'))
    with db.transaction() as conn:
        conn.execute(tenants.insert().values(id='org_synthetic', name='Synthetic research', settings={}, audit_head='0'*64, created_at=clock()))
        conn.execute(resources.insert().values(id='order-resource', tenant_id='org_synthetic', connection_id='synthetic-shop',
            kind='order', external_id='gid://shopify/Order/1', updated_at=clock(), data={
                'id': 'gid://shopify/Order/1', 'test': True, 'displayFulfillmentStatus': 'UNFULFILLED',
                'totalRefundedSet': {'shopMoney': {'amount': '0.00'}}}))
        conn.execute(resources.insert().values(id='variant-resource', tenant_id='org_synthetic', connection_id='synthetic-shop',
            kind='variant', external_id='gid://shopify/ProductVariant/1', updated_at=clock(), data={
                'id': 'gid://shopify/ProductVariant/1', 'sku': 'DEMO-BULB-01', 'inventoryQuantity': 3}))
        for kid, market, title in [('skill-de', 'DE', '德国能效规格'), ('skill-fr', 'FR', '法国本地退货'), ('skill-de-return', 'DE', '德国本地退货')]:
            conn.execute(knowledge.insert().values(id=kid, tenant_id='org_synthetic', title=title, market=market,
                content='人工核实政策与产品规格后回答。', evidence=['合成政策 v1'], active=True, created_at=clock()))
    actor = {'id': 'synthetic-operator', 'tenant_id': 'org_synthetic'}
    ops = Operations(db, None, None)
    task = ops.submit(actor, '德国客户的能效咨询', 'analyze', {'use_ai': True})
    yield db, ops, task
    db.engine.dispose()


def good(context):
    return {'finding': '只依据本角色提供的快照与政策进行分析。', 'evidence': context['evidence_ids'], 'limitations': ['这是同步快照，不是实时平台状态。']}


def metrics():
    return {'attempts': 1, 'input_tokens': 100, 'output_tokens': 40, 'format': 'json', 'model': 'synthetic-model', 'reused': False}


def test_explicit_order_scope_keeps_unrelated_orders_and_products_out_of_model(environment):
    db, ops, task = environment
    with db.transaction() as conn:
        conn.execute(resources.update().where(resources.c.id == 'order-resource').values(data={
            'id': 'gid://shopify/Order/1', 'name': '#1001', 'test': True,
            'displayFulfillmentStatus': 'UNFULFILLED', 'totalRefundedSet': {'shopMoney': {'amount': '0'}},
            'lineItems': {'nodes': [{'quantity': 1, 'variant': {'id': 'gid://shopify/ProductVariant/1', 'product': {'id': 'gid://shopify/Product/1'}}}], 'pageInfo': {'hasNextPage': False}}}))
        conn.execute(resources.update().where(resources.c.id == 'variant-resource').values(data={
            'id': 'gid://shopify/ProductVariant/1', 'product': {'id': 'gid://shopify/Product/1'},
            'sku': 'CASE-BULB', 'inventoryQuantity': 9}))
        conn.execute(resources.insert().values(id='unrelated-order', tenant_id=task['tenant_id'], connection_id='synthetic-shop',
            kind='order', external_id='gid://shopify/Order/2', updated_at=clock(), data={
                'id': 'gid://shopify/Order/2', 'name': '#1002', 'test': True,
                'displayFulfillmentStatus': 'FULFILLED', 'totalRefundedSet': {'shopMoney': {'amount': '10'}}}))
        conn.execute(resources.insert().values(id='unrelated-variant', tenant_id=task['tenant_id'], connection_id='synthetic-shop',
            kind='variant', external_id='gid://shopify/ProductVariant/2', updated_at=clock(), data={
                'id': 'gid://shopify/ProductVariant/2', 'product': {'id': 'gid://shopify/Product/2'},
                'sku': 'UNRELATED', 'inventoryQuantity': 1}))
    task = ops.submit({'id':'synthetic-operator','tenant_id':task['tenant_id']},
        '分析德国订单 #1001 的履约、库存与能效咨询', 'analyze', {'use_ai': True}, 'synthetic-shop')
    seen = []
    def model(system, context, evidence):
        seen.append(context)
        return good(context), metrics()
    result = AnalysisRunner(db, model).run(task)
    assert result['statistics']['order_count'] == 1 and result['statistics']['refunded_order_count'] == 0
    assert result['statistics']['variant_count'] == 1 and result['statistics']['low_stock_count'] == 0
    assert result['statistics']['scope']['order_names'] == ['#1001']
    assert 'gid://shopify/Order/2' not in repr(seen) and 'UNRELATED' not in repr(seen)
    assert result['reports'][0]['evidence'] == ['gid://shopify/Order/1']
    assert result['reports'][1]['evidence'] == ['gid://shopify/ProductVariant/1']
    inventory_context = next(c for c in seen if 'inventory' in c)
    assert inventory_context['inventory'] == [{'id':'gid://shopify/ProductVariant/1', 'sku':'CASE-BULB', 'aggregate_available':9, 'ordered_quantity':1}]
    assert 'gid://shopify/Order/1' not in repr(inventory_context)


def test_unknown_explicit_order_fails_before_any_model_call(environment):
    db, ops, old = environment
    task = ops.submit({'id':'synthetic-operator','tenant_id':old['tenant_id']},
        '分析订单 #9999', 'analyze', {'use_ai': True}, 'synthetic-shop')
    def unexpected(*args):
        raise AssertionError('An unresolved order must not reach the model')
    with pytest.raises(ValueError, match='ORDER_SCOPE_NOT_SYNCED'):
        AnalysisRunner(db, unexpected).run(task)


@pytest.mark.parametrize('incomplete', ['missing', 'truncated'])
def test_one_incomplete_order_cannot_hide_behind_another_complete_order(environment, incomplete):
    db, ops, old = environment
    complete = {'id':'gid://shopify/Order/1','name':'#1001','test':True,
        'displayFulfillmentStatus':'UNFULFILLED','totalRefundedSet':{'shopMoney':{'amount':'0'}},
        'lineItems':{'nodes':[{'quantity':1,'variant':{'id':'gid://shopify/ProductVariant/1','product':{'id':'gid://shopify/Product/1'}}}],
                     'pageInfo':{'hasNextPage':False}}}
    other = {**complete,'id':'gid://shopify/Order/2','name':'#1002'}
    if incomplete == 'missing':
        other.pop('lineItems')
    else:
        other['lineItems'] = {**complete['lineItems'],'pageInfo':{'hasNextPage':True}}
    with db.transaction() as conn:
        conn.execute(resources.update().where(resources.c.id=='order-resource').values(data=complete))
        conn.execute(resources.insert().values(id='incomplete-order',tenant_id=old['tenant_id'],connection_id='synthetic-shop',
            kind='order',external_id=other['id'],updated_at=clock(),data=other))
    task=ops.submit({'id':'synthetic-operator','tenant_id':old['tenant_id']},
        '分析订单 #1001 与 #1002 的库存','analyze',{'use_ai':True},'synthetic-shop')
    def unexpected(*args):
        raise AssertionError('Incomplete linked orders must not reach the model')
    with pytest.raises(ValueError,match='ORDER_PRODUCT_SCOPE_INCOMPLETE'):
        AnalysisRunner(db,unexpected).run(task)


def test_native_graph_parallel_nodes_receive_only_their_role_context(environment):
    db, ops, task = environment
    barrier = threading.Barrier(3)
    seen = []
    def model(system, context, evidence):
        seen.append(context)
        barrier.wait(timeout=5)
        return good(context), metrics()
    result = AnalysisRunner(db, model).run(task)
    assert len(seen) == 3 and result['analysis']['orchestrator'] == 'langgraph_native_fanout'
    assert result['analysis']['model_attempts_this_run'] == 3
    assert [r['role_key'] for r in result['reports']] == ['orders', 'inventory', 'knowledge']
    order_context = next(c for c in seen if 'order_count' in c.get('statistics', {}))
    assert 'knowledge' not in order_context and 'low_stock' not in order_context['statistics']
    policy_context = next(c for c in seen if 'knowledge' in c)
    assert 'statistics' not in policy_context and policy_context['evidence_ids'] == ['skill-de']
    assert 'skill-fr' not in repr(policy_context) and 'skill-de-return' not in repr(policy_context)
    assert len(db.list(analysis_steps, task['tenant_id'])) == 3 and db.verify(task['tenant_id'])['valid']


def test_process_restart_reuses_successful_roles_but_retries_missing_role(environment, monkeypatch):
    db, ops, task = environment
    def first(system, context, evidence):
        if 'variant_count' in context.get('statistics', {}):
            # A worker interruption is different from a handled provider error.
            raise RuntimeError('synthetic worker interruption')
        return good(context), metrics()
    monkeypatch.setattr(analysis, 'analyze_json', first)
    with pytest.raises(RuntimeError):
        ops.process_task(task['id'], task['tenant_id'])
    assert db.get(tasks, task['id'])['status'] == 'failed'
    done = {s['role'] for s in db.list(analysis_steps, task['tenant_id']) if s['status'] == 'done'}
    assert done == {'orders', 'knowledge'}
    assert db.get(tasks, task['id'])['result']['analysis']['completed_roles'] == 2
    url = db.url
    db.engine.dispose()
    restarted = Database(url)
    calls = []
    def second(system, context, evidence):
        calls.append(context)
        return good(context), metrics()
    with restarted.transaction() as conn:
        conn.execute(update(tasks).where(tasks.c.id == task['id']).values(status='queued'))
    monkeypatch.setattr(analysis, 'analyze_json', second)
    Operations(restarted, None, None).process_task(task['id'], task['tenant_id'])
    result = restarted.get(tasks, task['id'])['result']
    assert len(calls) == 1 and 'variant_count' in calls[0]['statistics']
    assert result['analysis']['reused_roles'] == 2 and result['analysis']['model_attempts_this_run'] == 1
    assert restarted.get(tasks, task['id'])['status'] == 'completed'
    assert restarted.verify(task['tenant_id'])['valid']
    restarted.engine.dispose()


def test_disable_or_change_policy_invalidates_only_affected_cached_role(environment):
    db, ops, task = environment
    calls = []
    def model(system, context, evidence):
        calls.append(context)
        return good(context), metrics()
    runner = AnalysisRunner(db, model)
    runner.run(task)
    with db.transaction() as conn:
        conn.execute(update(knowledge).where(knowledge.c.id == 'skill-de').values(active=False))
    result = runner.run(task)
    assert len(calls) == 4 and result['analysis']['reused_roles'] == 2
    assert result['statistics']['knowledge'] == [] and result['reports'][2]['evidence'] == []
    with db.transaction() as conn:
        conn.execute(update(resources).where(resources.c.id == 'variant-resource').values(data={
            'id': 'gid://shopify/ProductVariant/1', 'sku': 'DEMO-BULB-01', 'inventoryQuantity': 10}))
    result = runner.run(task)
    assert len(calls) == 5 and result['analysis']['reused_roles'] == 2
    assert result['statistics']['low_stock_count'] == 0


def test_malicious_role_output_falls_back_and_cannot_create_business_actions(environment):
    db, ops, task = environment
    def model(system, context, evidence):
        return {**good(context), 'refund': {'order_id': 'gid://shopify/Order/1', 'amount': '50'}}, metrics()
    result = AnalysisRunner(db, model).run(task)
    assert all(r['mode'] == 'rules_fallback' for r in result['reports'])
    from backend.production.database import actions
    assert db.list(actions, task['tenant_id']) == []
    assert all('refund' not in r['finding'] for r in result['reports'])


def test_handled_model_failure_is_explicit_and_uses_deterministic_rules(environment):
    db, ops, task = environment
    def model(*args):
        raise ModelFailure('MODEL_RATE_LIMIT', {**metrics(), 'attempts': 2})
    result = AnalysisRunner(db, model).run(task)
    assert result['analysis']['model_attempts_this_run'] == 6
    assert all(r['mode'] == 'rules_fallback' and r['fallback_reason'] == 'MODEL_RATE_LIMIT' for r in result['reports'])
    assert result['statistics']['unfulfilled_count'] == 1
