"""Official SP-API wire shapes, regional static fixtures and single-send writes."""
import json
import httpx
import pytest
from backend.production.platforms.amazon import AmazonAdapter, ORDER_PATH, OUTBOUND_PATH, SANDBOX_ORDER_FIXTURES, _TOKENS
from backend.production.platforms.contracts import PlatformError, CommerceAdapter


class SandboxHTTP:
    def __init__(self):
        self.stock={}
        self.orders={}
        self.calls=[]
        self.unknown=False

    def handle(self, request):
        self.calls.append(request)
        path=request.url.path
        if request.url.host=='api.amazon.com':
            return httpx.Response(200,json={'access_token':'synthetic-token','expires_in':3600})
        assert request.headers['x-amz-access-token']=='synthetic-token'
        if path==ORDER_PATH:
            return httpx.Response(200,json={'orders':[{'orderId':'000-0000000-0000000','createdTime':'2026-01-01T00:00:00Z',
                'lastUpdatedTime':'2026-01-02T00:00:00Z','buyer':{'buyerEmail':'private@example.invalid'},'recipient':{'deliveryAddress':{'name':'Private buyer'}},
                'fulfillment':{'fulfilledBy':'AMAZON','fulfillmentStatus':'SHIPPED'},'proceeds':{'grandTotal':{'amount':'19.90','currencyCode':'USD'}},
                'orderItems':[{'orderItemId':'line-1','quantityOrdered':1,'product':{'sellerSku':'TEST','asin':'SYNTHETIC'},'fulfillment':{'quantityFulfilled':1,'quantityUnfulfilled':0}}]}],
                'pagination':{'nextToken':'static-example-continuation'}})
        if path=='/fba/inventory/v1/summaries':
            requested=request.url.params.get('sellerSkus')
            items=[{'sellerSku':sku,'productName':'Synthetic item','inventoryDetails':{'fulfillableQuantity':qty}} for sku,qty in self.stock.items() if not requested or sku==requested]
            return httpx.Response(200,json={'payload':{'inventorySummaries':items}})
        body=json.loads(request.content or b'{}')
        if path=='/fba/inventory/v1/items':
            assert 'marketplaceId' in body and 'marketplaceIds' not in body
            self.stock[body['sellerSku']]=0
            return httpx.Response(200,json={})
        if path=='/fba/inventory/v1/items/inventory':
            assert request.headers.get('x-amzn-idempotency-token')
            for item in body['inventoryItems']:
                assert item['marketplaceId']=='ATVPDKIKX0DER'
                self.stock[item['sellerSku']]+=item['quantity']
            if self.unknown:raise httpx.ReadTimeout('response lost',request=request)
            return httpx.Response(200,json={})
        if path==OUTBOUND_PATH and request.method=='POST':
            assert body['destination']['deliveryAddress']['name']=='Synthetic Buyer'
            self.orders[body['orderId']]={'orderId':body['orderId'],'status':'PROCESSING','lineItems':body['lineItems']}
            return httpx.Response(200,json={})
        if path.startswith(OUTBOUND_PATH+'/'):
            identifier=path[len(OUTBOUND_PATH)+1:].split('/')[0]
            if '/packages/' in path:
                package=path.split('/packages/')[1]
                found=next(p for s in self.orders[identifier]['shipments'] for p in s['packages'] if p['packageId']==package)
                found['status']=body['status']
            elif path.endswith('/cancel'):
                self.orders[identifier]['status']='CANCELLED'
            elif path.endswith('/status'):
                self.orders[identifier]['status']=body['status']
                if body['status']=='COMPLETE':self.orders[identifier]['shipments']=[{'packages':[{'packageId':'SYNTHETIC-PACKAGE','status':'IN_TRANSIT'}]}]
            return httpx.Response(200,json={'order':self.orders[identifier]})
        raise AssertionError('unexpected endpoint')


@pytest.fixture
def amazon():
    _TOKENS.clear()
    fake=SandboxHTTP()
    api=AmazonAdapter({'client_id':'synthetic-client-id','client_secret':'synthetic-client-secret','refresh_token':'synthetic-refresh'},
        {'region':'na','marketplace_id':'ATVPDKIKX0DER','environment':'sandbox'},httpx.MockTransport(fake.handle))
    return api,fake


@pytest.mark.business
@pytest.mark.parametrize('region,market',[('na','ATVPDKIKX0DER'),('eu','A1PA6795UKMFR9'),('fe','A1VC38T7YXB528')])
def test_latest_orders_matching_region_static_page_and_privacy(amazon,region,market):
    old,fake=amazon
    api=AmazonAdapter(old.secret,dict(region=region,marketplace_id=market,environment='sandbox'),old.transport)
    result=api.snapshot_orders({})
    assert isinstance(api,CommerceAdapter)
    assert len(result)==1 and result[0]['environment']=='sandbox_static'
    calls=[r for r in fake.calls if r.url.path==ORDER_PATH]
    assert len(calls)==1 and calls[0].url.host==f'sandbox.sellingpartnerapi-{region}.amazon.com'
    assert calls[0].url.params['marketplaceIds']==SANDBOX_ORDER_FIXTURES[region]['marketplaceIds']
    text=json.dumps(result)
    assert 'private@example.invalid' not in text and 'Private buyer' not in text and 'recipient' not in text
    assert api.capabilities()['orders_version']=='2026-01-01'


@pytest.mark.business
def test_dynamic_sandbox_sku_stock_mcf_complete_and_cancel_readback(amazon):
    api,fake=amazon
    sku='xiaokua-test-contract'
    before,p=api.prepare_action('sandbox_sku',{'sku':sku},True)
    assert api.execute_action('sandbox_sku',p,before,'sku-action',True)['observed']['quantity']==0
    before,p=api.prepare_action('sandbox_stock',{'sku':sku,'quantity':8},True)
    result=api.execute_action('sandbox_stock',p,before,'stock-action',True)
    assert result['expected_quantity']==8 and not result['pending_verification']
    before,p=api.prepare_action('mcf_create',{'sku':sku,'quantity':2},True)
    order=api.execute_action('mcf_create',p,before,'mcf-action',True)
    assert order['status']=='PROCESSING' and not order['pending_verification']
    matched,proof=api.verify_action('mcf_create',dict(p,_action_id='mcf-action'))
    assert matched and proof['order_id']==order['order_id']
    before,p=api.prepare_action('sandbox_status',{'order_id':order['order_id'],'status':'COMPLETE'},True)
    complete=api.execute_action('sandbox_status',p,before,'status-action',True)
    assert complete['status']=='COMPLETE'
    for status in ('OUT_FOR_DELIVERY','DELIVERED'):
        before,p=api.prepare_action('sandbox_package',{'order_id':order['order_id'],'package_id':'SYNTHETIC-PACKAGE','status':status},True)
        result=api.execute_action('sandbox_package',p,before,'package-'+status,True)
        assert not result['pending_verification'] and result['packages'][0]['status']==status
    with pytest.raises(PlatformError,match='TRANSITION_INVALID'):
        api.prepare_action('sandbox_package',{'order_id':order['order_id'],'package_id':'SYNTHETIC-PACKAGE','status':'IN_TRANSIT'},True)
    before,p=api.prepare_action('mcf_create',{'sku':sku,'quantity':1},True)
    second=api.execute_action('mcf_create',p,before,'mcf-action-2',True)
    before,p=api.prepare_action('mcf_cancel',{'order_id':second['order_id']},True)
    assert api.execute_action('mcf_cancel',p,before,'cancel-action',True)['status']=='CANCELLED'
    assert api.inventory_position('amazon:sku:'+sku,'amazon:fba:ATVPDKIKX0DER')['pool_type']=='platform'


@pytest.mark.security
def test_amazon_write_timeout_is_single_send_and_read_only_reconcile(amazon):
    api,fake=amazon
    sku='xiaokua-test-response-loss';fake.stock[sku]=10
    before,p=api.prepare_action('sandbox_stock',{'sku':sku,'quantity':3},True)
    fake.unknown=True
    with pytest.raises(PlatformError) as error:api.execute_action('sandbox_stock',p,before,'write-once',True)
    assert error.value.unknown
    assert len([r for r in fake.calls if r.method=='POST' and r.url.path.endswith('/items/inventory')])==1
    assert api.verify_action('sandbox_stock',dict(p,_expected_quantity=13))[0]


@pytest.mark.security
def test_amazon_production_write_guard_region_and_changed_state(amazon):
    api,fake=amazon;fake.stock['xiaokua-test-state']=10
    before,p=api.prepare_action('sandbox_stock',{'sku':'xiaokua-test-state','quantity':1},True)
    fake.stock[p['sku']]=9
    with pytest.raises(PlatformError,match='AMAZON_STATE_CHANGED'):api.execute_action('sandbox_stock',p,before,'stale',True)
    assert not [r for r in fake.calls if r.url.path.endswith('/items/inventory')]
    prod=AmazonAdapter(api.secret,{'region':'na','marketplace_id':'ATVPDKIKX0DER','environment':'production'},api.transport)
    assert prod.capabilities()['operations']==['sync','analyze','chat']
    with pytest.raises(PlatformError,match='OPERATION_NOT_OPEN'):prod.prepare_action('mcf_create',{'sku':'TEST','quantity':1},False)
    with pytest.raises(PlatformError,match='REGION_MISMATCH'):AmazonAdapter(api.secret,{'region':'na','marketplace_id':'A1VC38T7YXB528'},api.transport)


@pytest.mark.security
def test_amazon_does_not_request_production_identity_datasets(amazon):
    api,fake=amazon
    prod=AmazonAdapter(api.secret,{'region':'na','marketplace_id':'ATVPDKIKX0DER','environment':'production'},api.transport)
    # Use a single real-style page to test production pagination semantics.
    def handler(request):
        response=fake.handle(request)
        if request.url.path==ORDER_PATH:
            data=response.json();data.pop('pagination');return httpx.Response(200,json=data)
        return response
    prod.transport=httpx.MockTransport(handler)
    result=prod.snapshot_orders({})
    call=next(r for r in fake.calls if r.url.path==ORDER_PATH)
    assert 'BUYER' not in call.url.params['includedData'] and 'RECIPIENT' not in call.url.params['includedData']
    assert result[0]['environment']=='production'


@pytest.mark.security
@pytest.mark.parametrize('kind',['sandbox_sku','sandbox_stock','mcf_create','mcf_cancel','sandbox_status','sandbox_package'])
def test_feishu_amazon_approval_preview_contains_only_business_fields(kind):
    from backend.production.feishu import approval_preview
    text=approval_preview({'kind':kind,'before':{},'payload':{'sku':'Synthetic-SKU','quantity':2,'order_id':'synthetic-order','package_id':'synthetic-package',
        'status':'DELIVERED','parent_command':'hidden-internal','client_secret':'never-display'}})
    assert 'Amazon 动态沙箱' in text and 'Synthetic-SKU' in text
    assert 'hidden-internal' not in text and 'never-display' not in text
