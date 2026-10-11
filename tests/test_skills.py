"""Market/topic boundaries, bounded loading and portable skill downloads."""
import io
import json
import re
import zipfile
import pytest
from fastapi.testclient import TestClient
from backend.demo import create_demo, DEMO_PASSWORD, DEMO_ORG
from backend.production.database import knowledge, tenants, clock
from backend.production.skills import retrieve, markdown, skill_name, bundle

pytestmark = pytest.mark.memory


@pytest.fixture
def app(tmp_path):
    value = create_demo(tmp_path, worker_enabled=False)
    with value.state.db.transaction() as conn:
        for kid, market, title, active in [('de-energy', 'DE', '德国能效政策', True),
                ('de-return', 'DE', '德国本地退货', True), ('fr-return', 'FR', '法国本地退货', True),
                ('de-draft', 'DE', '德国能效草稿', False), ('gb-return', 'GB', '英国退货政策', True)]:
            conn.execute(knowledge.insert().values(id=kid, tenant_id=DEMO_ORG, title=title, market=market,
                content='已确认的合成政策；未知规格转人工。', evidence=['合成规格 v1'], active=active, created_at=clock()))
    yield value
    value.state.db.engine.dispose()


@pytest.mark.parametrize('query,expected', [('德国客户能效', {'de-energy'}),
    ('German energy efficiency', {'de-energy'}), ('FR 本地退货', {'fr-return'}),
    ('英国退货', {'gb-return'}), ('分析最近订单', set())])
def test_metadata_retrieval_matches_region_and_topic_and_excludes_drafts(app, query, expected):
    selected, meta = retrieve(app.state.db, DEMO_ORG, query)
    assert {k['id'] for k in selected} == expected
    assert meta['strategy'] == 'metadata_then_body'
    assert meta['selected'] <= 4


def test_retrieval_budget_is_bounded_even_when_many_policies_match(app):
    with app.state.db.transaction() as conn:
        for i in range(20):
            conn.execute(knowledge.insert().values(id=f'synthetic-long-policy-{i}', tenant_id=DEMO_ORG,
                title='德国能效规格', market='DE', content='合成规格' * 2000,
                evidence=['synthetic'], active=True, created_at=clock()))
    selected, meta = retrieve(app.state.db, DEMO_ORG, '德国能效')
    assert len(selected) == 4 and all(len(k['content']) <= 1600 for k in selected)
    assert sum(len(k['content']) for k in selected) <= 6400


def test_skill_export_has_valid_metadata_and_an_inert_safe_bundle(app):
    k = app.state.db.get(knowledge, 'de-draft', DEMO_ORG)
    k['market'] = 'DE\nmalicious-field: injected'
    name = skill_name(k)
    content = markdown(k)
    header = content.split('---\n')[1]
    assert re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) and len(name) <= 64
    assert 'description: ' in header and 'status: "draft"' in header
    # User-controlled newlines are quoted JSON/YAML scalars, not extra fields.
    market = next(line.split(': ', 1)[1] for line in header.splitlines() if line.startswith('  market:'))
    assert json.loads(market) == k['market']
    assert '\nmalicious-field:' not in header
    bundle_name, data = bundle(k)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.namelist() == [name+'/SKILL.md', name+'/references/evidence.md']
        assert 'references/evidence.md' in archive.read(name+'/SKILL.md').decode()
        assert '合成规格 v1' in archive.read(name+'/references/evidence.md').decode()
        assert not any('..' in p or p.startswith('/') or p.endswith('.py') for p in archive.namelist())
    assert bundle_name == name


def test_bundle_download_requires_login_and_current_tenant(app):
    with app.state.db.transaction() as conn:
        conn.execute(tenants.insert().values(id='other-synthetic-org', name='Other synthetic org',
            settings={}, audit_head='0'*64, created_at=clock()))
        conn.execute(knowledge.insert().values(id='foreign-skill', tenant_id='other-synthetic-org',
            title='foreign', market='DE', content='synthetic', evidence=['synthetic'], active=True, created_at=clock()))
    with TestClient(app) as client:
        assert client.get('/api/knowledge/de-energy/bundle').status_code == 401
        token = client.get('/api/bootstrap').json()['csrf']
        assert client.post('/api/auth/login', json={'email': 'owner@example.invalid', 'password': DEMO_PASSWORD},
            headers={'X-CSRF-Token': token}).status_code == 200
        result = client.get('/api/knowledge/de-energy/bundle')
        assert result.status_code == 200 and result.headers['content-type'] == 'application/zip'
        assert client.get('/api/knowledge/foreign-skill/bundle').status_code == 404
        assert client.get('/api/knowledge/de-energy/download').text.startswith('---\nname: xiaokua-')
