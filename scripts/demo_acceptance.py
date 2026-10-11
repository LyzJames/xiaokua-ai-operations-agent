"""Reproduce the full synthetic workflow, without any platform/model HTTP calls."""
import argparse
import csv
import json
import io
import zipfile
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi.testclient import TestClient
from backend.demo import create_demo, DEMO_PASSWORD, DEMO_ORG, SHOP_CONNECTION
from backend.production.database import actions, tasks, jobs, analysis_steps


def run():
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append({'name': name, 'passed': True})
    def no_network(*args, **kwargs):
        raise AssertionError('Demo attempted an external HTTP request')
    with tempfile.TemporaryDirectory(prefix='xiaokua-demo-') as temporary, patch.object(httpx.HTTPTransport, 'handle_request', no_network):
        directory = Path(temporary)
        app = create_demo(directory, worker_enabled=False)
        db = app.state.db
        with TestClient(app) as owner_client, TestClient(app) as approver_client:
            client = owner_client
            clients = {'owner': owner_client, 'approver': approver_client}
            def post(path, body):
                csrf = client.get('/api/bootstrap').json()['csrf']
                return client.post('/api' + path, json=body, headers={'X-CSRF-Token': csrf})
            def login(role):
                nonlocal client
                client = clients[role]
                if client.get('/api/bootstrap').json().get('user'):
                    return
                response = post('/auth/login', {'email': role+'@example.invalid', 'password': DEMO_PASSWORD})
                assert response.status_code == 200, response.text
            def drain():
                for _ in range(100):
                    if not app.state.worker.run_once():
                        return
                raise AssertionError('Worker did not settle')
            def propose(kind, params):
                login('owner')
                response = post('/tasks', {'command': '合成流程验收：'+kind, 'kind': kind,
                    'connection_id': SHOP_CONNECTION, 'params': params})
                assert response.status_code == 201, response.text
                drain()
                return db.list(actions, DEMO_ORG, 1, task_id=response.json()['id'])[0]
            def decide(action, decision='approve'):
                login('approver')
                response = post('/demo/events', {'action_id': action['id'], 'decision': decision})
                assert response.status_code == 200, response.text
                drain()
                return db.get(actions, action['id'])
            check('unauthenticated outbox denied', client.get('/api/demo/outbox').status_code == 401)
            login('owner')
            check('CSRF required for simulated events', client.post('/api/demo/events', json={'text':'同步订单'}).status_code == 403)
            check('real credential import blocked', post('/shopify/import-local', {}).status_code == 403)
            check('simulated message accepted', post('/demo/events', {'text':'小跨，同步昨晚的欧洲订单与库存'}).status_code == 200)
            drain()
            state = client.get('/api/state').json()
            check('two test orders and one variant synchronized', len(state['orders']) == 2 and len(state['variants']) == 1 and all(o['data']['test'] for o in state['orders']))
            check('message receives task and completion notifications', len(client.get('/api/demo/outbox').json()['messages']) == 2)
            inventory = propose('inventory', {'item_id':'gid://shopify/InventoryItem/1','location_id':'gid://shopify/Location/1','quantity':15})
            check('inventory before/after proposal persisted', inventory['before']['quantity'] == 10 and inventory['payload']['quantity'] == 15)
            check('applicant cannot approve own action', post('/approvals/'+inventory['id'], {'approved':True}).status_code == 422)
            check('proposal does not change inventory', app.state.demo_shop.data['quantity'] == 10)
            check('independent card approval executes', decide(inventory)['status'] == 'executed' and app.state.demo_shop.data['quantity'] == 15)
            decide(inventory)
            check('duplicate card does not repeat write', app.state.demo_shop.data['writes'].count(inventory['id']) == 1)
            check('duplicate returns already processed notification', any('操作已处理' in m['content'].get('text','') for m in client.get('/api/demo/outbox').json()['messages']))
            original = app.state.demo_shop.data['description']
            rejected = propose('product', {'product_id':'gid://shopify/Product/1','description':'拒绝示例：未经核对的能效说明'})
            check('rejected description never written', decide(rejected,'reject')['status'] == 'rejected' and app.state.demo_shop.data['description'] == original)
            accepted = propose('product', {'product_id':'gid://shopify/Product/1','description':'合成演示说明：请查阅已确认的产品规格。'})
            check('approved description is escaped and written', decide(accepted)['status'] == 'executed' and app.state.demo_shop.data['description'].startswith('<p>合成演示说明'))
            refund = propose('refund', {'order_id':'gid://shopify/Order/1','amount':'0.50','currency':'USD'})
            decide(refund)
            order = app.state.demo_shop.order('gid://shopify/Order/1')
            check('synthetic refund is exactly 0.50 USD', order['totalRefundedSet']['shopMoney']['amount'] == '0.50' and order['netPaymentSet']['shopMoney']['amount'] == '19.40')
            fulfilled = propose('fulfill', {'order_id':'gid://shopify/Order/2','company':'DEMO-CARRIER','number':'DEMO-TRACK-001'})
            check('fulfillment succeeds after approval', decide(fulfilled)['status'] == 'executed')
            tracking = propose('tracking', {'order_id':'gid://shopify/Order/2','fulfillment_id':'gid://shopify/Fulfillment/1','company':'DEMO-CARRIER','number':'DEMO-TRACK-002'})
            decide(tracking)
            check('tracking update is read back', app.state.demo_shop.order('gid://shopify/Order/2')['fulfillments'][0]['trackingInfo'][0]['number'] == 'DEMO-TRACK-002')
            canceled = propose('cancel', {'order_id':'gid://shopify/Order/1','reason':'OTHER'})
            decide(canceled)
            order = app.state.demo_shop.order('gid://shopify/Order/1')
            check('cancel does not trigger another refund', bool(order['cancelledAt']) and order['totalRefundedSet']['shopMoney']['amount'] == '0.50')
            login('owner')
            with (ROOT/'examples'/'support-synthetic.csv').open(encoding='utf-8-sig',newline='') as fixture:
                records = list(csv.DictReader(fixture))
            response = post('/support/import', {'records':records})
            check('ten synthetic support records imported', response.status_code == 200 and response.json()['count'] == 10)
            for _ in range(2):
                response = post('/tasks', {'command':'从合成客服记录学习','kind':'learn','params':{}})
                assert response.status_code == 201, response.text
                drain()
            notes = client.get('/api/state').json()['knowledge']
            check('learning creates two inactive drafts without duplicates', len(notes) == 2 and all(not k['active'] for k in notes))
            for note in notes:
                assert post('/knowledge/'+note['id']+'/toggle', {'active':True}).status_code == 200
            skill = client.get('/api/knowledge/'+notes[0]['id']+'/download')
            check('skill export contains evidence and market', skill.status_code == 200 and '## 证据' in skill.text and 'market:' in skill.text)
            package = client.get('/api/knowledge/'+notes[0]['id']+'/bundle')
            with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
                paths=archive.namelist()
                check('portable skill bundle contains SKILL.md and references', package.status_code==200 and len(paths)==2 and paths[0].endswith('/SKILL.md') and paths[1].endswith('/references/evidence.md'))
            response = post('/demo/events', {'text':'分析德国客户的能效咨询'})
            assert response.status_code == 200, response.text
            drain()
            task = db.list(tasks, DEMO_ORG, 1)[0]
            check('three role reports explicitly use rules', len(task['result']['reports']) == 3 and all(r['mode'] == 'rules' for r in task['result']['reports']))
            check('native role graph exposes complete progress with no model calls', task['result']['analysis']['completed_roles']==3 and task['result']['analysis']['model_attempts_this_run']==0)
            check('role results are persisted independently', len(db.list(analysis_steps,DEMO_ORG,task_id=task['id']))==3)
            selected = task['result']['statistics']['knowledge']
            check('German query excludes French knowledge', len(selected) == 1 and selected[0]['title'].startswith('DE '))
            check('knowledge context follows bounded metadata-first retrieval', task['result']['retrieval']['strategy']=='metadata_then_body' and task['result']['retrieval']['selected']==1)
            report = client.get('/api/tasks/'+task['id']+'/report')
            check('Markdown report export succeeds', report.status_code == 200 and '客服知识 Agent' in report.text)
            audit = client.get('/api/audit/export')
            check('audit CSV export succeeds', audit.status_code == 200 and 'Previous Hash' in audit.text)
            check('audit chain verifies', db.verify(DEMO_ORG)['valid'])
            check('no failed background jobs', all(j['status'] == 'done' for j in db.list(jobs, DEMO_ORG, 10000)))
            queued = post('/tasks', {'command':'重启后恢复只读同步','kind':'sync','connection_id':SHOP_CONNECTION,'params':{}}).json()['id']
        # Reopen the same demo directory; the queued job and commerce state survive.
        restarted = create_demo(directory, worker_enabled=False)
        with TestClient(restarted):
            for _ in range(30):
                if not restarted.state.worker.run_once():
                    break
            check('restart recovers queued sync and inventory', restarted.state.db.get(tasks,queued)['status'] == 'completed' and restarted.state.demo_shop.data['quantity'] == 15)
            check('audit remains valid after restart', restarted.state.db.verify(DEMO_ORG)['valid'])
    return {'schema':2,'captured_at':datetime.now(timezone.utc).isoformat(),'synthetic':True,
        'platform_http_calls':0,'model_http_calls':0,'checks_passed':len(checks),'checks':checks}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    report = run()
    text = json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text+'\n',encoding='utf-8')
    print(text)
