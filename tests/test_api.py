from fastapi.testclient import TestClient
from backend.main import create_app

def test_session_and_csrf_boundaries(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        assert client.get('/api/health').status_code == 200
        assert client.get('/api/state').status_code == 401
        csrf = client.get('/api/bootstrap').json()['csrf']
        response = client.get('/api/state')
        assert response.status_code == 200
        assert 'DEEPSEEK_API_KEY' not in response.text
        assert 'FEISHU_APP_SECRET' not in response.text
        assert client.post('/api/tasks', json={'command':'hello'}).status_code == 403
        assert client.post('/api/tasks', headers={'X-CSRF-Token': csrf, 'Origin':'https://malicious.example'}, json={'command':'hello'}).status_code == 403
        response = client.post('/api/tasks', headers={'X-CSRF-Token': csrf}, json={'command':'处理订单','kind':'orders'})
        assert response.status_code == 201
        assert client.post('/api/tasks', headers={'X-CSRF-Token':csrf}, json={'command':'bad','kind':'purchase','params':{'amount':'NaN'}}).status_code == 422
        assert client.get('/api/audit/export').status_code == 200
        assert client.get('/api/unknown').status_code == 404

def test_bot_requires_service_token_and_allowlist(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        payload = {'event_id':'e1','sender_id':'ou_unknown','command':'处理订单'}
        assert client.post('/api/bot/tasks',json=payload).status_code == 401
        token = (tmp_path / 'service.token').read_text()
        assert client.post('/api/bot/tasks',headers={'X-Xiaokua-Service':token},json=payload).status_code == 403
