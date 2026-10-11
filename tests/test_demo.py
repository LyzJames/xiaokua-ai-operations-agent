"""The public demo must exercise real workflow code without platform/model calls."""
from fastapi.testclient import TestClient
import httpx
import pytest
from backend.demo import create_demo, DEMO_PASSWORD, DEMO_ORG, SHOP_CONNECTION
from backend.production.database import actions, tasks, jobs


def post(client, path, body):
    csrf = client.get('/api/bootstrap').json()['csrf']
    return client.post('/api' + path, json=body, headers={'X-CSRF-Token': csrf})


def login(client, role):
    response = post(client, '/auth/login', {'email': role + '@example.invalid', 'password': DEMO_PASSWORD})
    assert response.status_code == 200, response.text


def drain(app):
    for _ in range(30):
        if not app.state.worker.run_once():
            break
    else:
        raise AssertionError('Demo jobs did not settle')
    assert not [j for j in app.state.db.list(jobs, DEMO_ORG) if j['status'] == 'failed']


def no_external_calls(*args, **kwargs):
    raise AssertionError('Offline demo attempted a platform/model HTTP request')


def test_demo_routing_auth_csrf_and_external_connection_guards(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_external_calls)
    monkeypatch.setenv('SHOPIFY_ACCESS_TOKEN', 'private-credential-must-not-be-used')
    app = create_demo(tmp_path, worker_enabled=False)
    # TestClient uses its own ASGI transport, without an external socket.
    with TestClient(app) as client:
        assert client.get('/demo/feishu').status_code == 200
        assert '离线研究演示' in client.get('/demo/feishu').text
        assert client.get('/demo/feishu.js').headers['content-type'].startswith('application/javascript')
        assert client.get('/api/bootstrap').json()['demo'] is True
        assert client.get('/api/demo/outbox').status_code == 401
        assert client.post('/api/demo/events', json={'text': '同步订单'}).status_code == 401
        login(client, 'operator')
        assert client.post('/api/demo/events', json={'text': '同步订单'}).status_code == 403
        for path in ('/shopify/import-local', '/feishu/import-local', '/feishu/bind'):
            assert post(client, path, {}).status_code == 403
        assert client.post('/hooks/feishu/conn_demo_feishu', json={}).status_code == 403
        assert 'private-credential-must-not-be-used' not in client.get('/api/state').text


def test_demo_feishu_event_runs_worker_sync_and_three_roles_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_external_calls)
    app = create_demo(tmp_path, worker_enabled=False)
    with TestClient(app) as client:
        login(client, 'operator')
        assert post(client, '/demo/events', {'text': '小跨，同步订单'}).status_code == 200
        drain(app)
        state = client.get('/api/state').json()
        assert len(state['orders']) == 2 and len(state['variants']) == 1
        assert all(o['data']['test'] for o in state['orders'])
        assert state['tasks'][0]['status'] == 'completed'
        assert len(client.get('/api/demo/outbox').json()['messages']) == 2
        assert post(client, '/demo/events', {'text': '分析德国客户的能效咨询'}).status_code == 200
        drain(app)
        task = app.state.db.list(tasks, DEMO_ORG, 1)[0]
        assert len(task['result']['reports']) == 3
        assert all(r['mode'] == 'rules' for r in task['result']['reports'])
        assert task['result']['source'] == '研究演示合成数据'
        assert app.state.db.verify(DEMO_ORG)['valid']


def test_demo_independent_card_approval_duplicate_and_persistent_inventory(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', no_external_calls)
    app = create_demo(tmp_path, worker_enabled=False)
    with TestClient(app) as client:
        login(client, 'owner')
        body = {'command': '合成库存 10 → 15', 'kind': 'inventory', 'connection_id': SHOP_CONNECTION,
                'params': {'item_id': 'gid://shopify/InventoryItem/1', 'location_id': 'gid://shopify/Location/1', 'quantity': 15}}
        assert post(client, '/tasks', body).status_code == 201
        drain(app)
        action = app.state.db.list(actions, DEMO_ORG, 1)[0]
        assert post(client, '/approvals/' + action['id'], {'approved': True}).status_code == 422
        assert app.state.demo_shop.data['quantity'] == 10
        assert post(client, '/auth/logout', {}).status_code == 200
        login(client, 'approver')
        assert len(client.get('/api/demo/outbox').json()['messages']) == 1
        event = {'action_id': action['id'], 'decision': 'approve'}
        assert post(client, '/demo/events', event).status_code == 200
        drain(app)
        assert app.state.db.get(actions, action['id'])['status'] == 'executed'
        assert app.state.demo_shop.data['quantity'] == 15
        assert post(client, '/demo/events', event).status_code == 200
        drain(app)
        assert len(app.state.demo_shop.data['writes']) == 1
        assert any('操作已处理' in m['content'].get('text', '') for m in client.get('/api/demo/outbox').json()['messages'])
        assert app.state.db.verify(DEMO_ORG)['valid']
    restarted = create_demo(tmp_path, worker_enabled=False)
    with TestClient(restarted) as client:
        login(client, 'owner')
        assert restarted.state.demo_shop.data['quantity'] == 15
        assert restarted.state.db.get(actions, action['id'])['status'] == 'executed'


def test_demo_refuses_non_demo_database(tmp_path):
    (tmp_path / 'product.sqlite').write_bytes(b'private-database')
    with pytest.raises(RuntimeError, match='非演示数据库'):
        create_demo(tmp_path, worker_enabled=False)
    assert (tmp_path / 'product.sqlite').read_bytes() == b'private-database'
