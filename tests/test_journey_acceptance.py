"""Protect real-journey provenance and the verifier's actual HTTP lifecycle."""
import json
from types import SimpleNamespace
import httpx
import pytest
from scripts import journey_acceptance as journey


@pytest.mark.reliability
def test_authenticated_report_client_can_export_and_is_closed(tmp_path, monkeypatch):
    (tmp_path/'accounts.json').write_text(json.dumps({'email':'owner@example.invalid','password':'synthetic-only-password'}))
    called = []
    def handle(request):
        called.append((request.method, request.url.path))
        if request.url.path == '/api/bootstrap':
            return httpx.Response(200, json={'csrf':'synthetic-csrf'})
        if request.url.path == '/api/auth/login':
            assert request.headers['X-CSRF-Token'] == 'synthetic-csrf'
            return httpx.Response(200, json={'user':{'id':'synthetic-owner'}})
        return httpx.Response(200, text='# Synthetic authenticated report')
    original = httpx.Client
    def factory(**options):
        return original(**options, transport=httpx.MockTransport(handle))
    monkeypatch.setattr(journey.httpx, 'Client', factory)
    with journey.client({'port':8005}, tmp_path) as client:
        assert client.get('/api/tasks/synthetic/report').text.startswith('# Synthetic')
    assert client.is_closed
    assert called == [('GET','/api/bootstrap'),('POST','/api/auth/login'),('GET','/api/tasks/synthetic/report')]


@pytest.mark.business
@pytest.mark.parametrize('source,test_transaction,accepted', [('web',True,True),('api',True,False),('web',False,False)])
def test_api_created_or_non_test_order_cannot_pass_buyer_checkout_gate(tmp_path, source, test_transaction, accepted):
    class ReadOnlyShop:
        def gql(self, *args):
            return {'orders':{'nodes':[{'id':'gid://shopify/Order/1','name':'#1001','sourceName':source,'test':True,
                'lineItems':{'nodes':[{'variant':{'id':'gid://shopify/ProductVariant/1'},'quantity':1}]},
                'transactions':[{'kind':'SALE','status':'SUCCESS','test':test_transaction,'gateway':'bogus'}]}]}}
        def inventory(self, *args):
            return {'inventoryLevel':{'quantities':[{'quantity':9}]}}
    context = {'tenant_id':'synthetic-org','shopify_connection':'synthetic-connection','cases':{'fulfillment':{
        'variant_id':'gid://shopify/ProductVariant/1','item_id':'gid://shopify/InventoryItem/1',
        'location_id':'gid://shopify/Location/1','steps':[],'refs':{}}}}
    services = (None,None,SimpleNamespace(client=lambda *args:ReadOnlyShop()),None,None)
    path = tmp_path/'context.json'
    if accepted:
        journey.bind_checkout(path,context,services,'fulfillment')
    else:
        with pytest.raises(RuntimeError,match='Checkout provenance'):
            journey.bind_checkout(path,context,services,'fulfillment')
    recorded = json.loads(path.read_text())
    assert recorded['cases']['fulfillment']['steps'][0]['passed'] is accepted
    assert recorded['cases']['fulfillment']['order_id'] == 'gid://shopify/Order/1'
