"""Fixed-origin Shopify adapter. No arbitrary GraphQL or URLs accepted from users/models."""
import time
import httpx
from sqlalchemy import select, update
from .database import connections, resources, clock, uid, row
from .security import shop_domain
from .platforms.contracts import PlatformError

API_VERSION = "2026-10"
SCOPES = "read_orders,write_orders,read_products,write_products,read_inventory,write_inventory,read_locations,read_merchant_managed_fulfillment_orders,write_merchant_managed_fulfillment_orders"

class ShopifyError(PlatformError):
    def __init__(self, code, unknown=False):
        super().__init__(code, unknown)
        self.args = ("Shopify 请求未完成："+code,)

def exchange(domain, values):
    domain = shop_domain(domain)
    try:
        with httpx.Client(timeout=25, trust_env=False) as client:
            r = client.post(f"https://{domain}/admin/oauth/access_token", data=values)
            if r.status_code != 200: raise ShopifyError("TOKEN_"+str(r.status_code))
            value = r.json()
            if not value.get("access_token"): raise ShopifyError("TOKEN_INVALID")
            return value
    except (httpx.HTTPError, ValueError): raise ShopifyError("TOKEN_UNREACHABLE") from None

class Shopify:
    def __init__(self, domain, token, transport=None):
        self.domain, self.token = shop_domain(domain), token
        self.transport = transport

    def gql(self, query, variables=None, mutation=False):
        for attempt in range(3 if not mutation else 1):
            try:
                with httpx.Client(timeout=30, trust_env=False, transport=self.transport) as client:
                    r = client.post(f"https://{self.domain}/admin/api/{API_VERSION}/graphql.json",headers={"X-Shopify-Access-Token":self.token},json={"query":query,"variables":variables or {}})
                if r.status_code == 429 and not mutation:
                    time.sleep(min(float(r.headers.get("Retry-After","1")),3)); continue
                if r.status_code >= 500 and not mutation:
                    time.sleep(attempt+1); continue
                if r.status_code != 200: raise ShopifyError("HTTP_"+str(r.status_code), mutation and r.status_code >= 500)
                result = r.json()
                self.last_graphql_errors = result.get("errors",[])
                if result.get("errors"):
                    code = str(result["errors"][0].get("extensions",{}).get("code","GRAPHQL_ERROR"))
                    if code == "THROTTLED" and not mutation:
                        time.sleep(attempt+1); continue
                    # An error on the selected response object may follow a successful mutation.
                    # Treat partial/unknown mutation outcomes as uncertain, never as a safe retry.
                    ambiguous=mutation and any(len(e.get("path",[]))>1 for e in result["errors"])
                    raise ShopifyError(code,ambiguous)
                return result["data"]
            except (httpx.HTTPError, ValueError, KeyError):
                if mutation: raise ShopifyError("TRANSPORT_UNKNOWN",True) from None
                if attempt == 2: raise ShopifyError("UNREACHABLE") from None
                time.sleep(attempt+1)
        raise ShopifyError("RATE_LIMIT")

    def capability(self):
        return self.gql("{shop{id name currencyCode myshopifyDomain plan{displayName partnerDevelopment}} currentAppInstallation{accessScopes{handle}}}")

    def paged(self, query, path, variables=None):
        cursor = None
        collected = []
        for _ in range(100):
            v = dict(variables or {}, cursor=cursor)
            page = self.gql(query,v)[path]
            collected.extend(page["nodes"])
            if not page["pageInfo"]["hasNextPage"]: return collected
            cursor = page["pageInfo"]["endCursor"]
        raise ShopifyError("PAGINATION_LIMIT_USE_NARROWER_DATE_RANGE")

    def orders(self, search=""):
        return self.paged('''query($cursor:String,$search:String){orders(first:100,after:$cursor,query:$search,sortKey:CREATED_AT){nodes{
          id name test createdAt updatedAt cancelledAt tags currencyCode displayFinancialStatus displayFulfillmentStatus
          totalPriceSet{shopMoney{amount currencyCode}} totalRefundedSet{shopMoney{amount currencyCode}}
          netPaymentSet{shopMoney{amount currencyCode}} fulfillments{id status trackingInfo{number company url}
            fulfillmentLineItems(first:100){nodes{quantity lineItem{id}} pageInfo{hasNextPage}}}
          lineItems(first:100){nodes{id quantity sku variant{id product{id}}} pageInfo{hasNextPage}}
        } pageInfo{hasNextPage endCursor}}}''',"orders",{"search":search})

    def variants(self):
        return self.paged('''query($cursor:String){productVariants(first:100,after:$cursor){nodes{
          id title sku price inventoryQuantity product{id title status tags descriptionHtml updatedAt}
          inventoryItem{id tracked inventoryLevels(first:30){nodes{location{id name} quantities(names:["available"]){name quantity}} pageInfo{hasNextPage}}}
        } pageInfo{hasNextPage endCursor}}}''',"productVariants")

    def order(self, order_id):
        data = self.gql('''query($id:ID!){order(id:$id){id name test tags updatedAt cancelledAt currencyCode displayFinancialStatus displayFulfillmentStatus
          totalPriceSet{shopMoney{amount currencyCode}} totalRefundedSet{shopMoney{amount currencyCode}} netPaymentSet{shopMoney{amount currencyCode}}
          transactions{id kind status gateway test amountSet{shopMoney{amount currencyCode}} parentTransaction{id}}
          lineItems(first:100){nodes{id quantity sku variant{id}} pageInfo{hasNextPage}}
          fulfillments{id status trackingInfo{number company url}}
          refunds{id note transactions(first:100){nodes{id status amountSet{shopMoney{amount currencyCode}}}}}
          fulfillmentOrders(first:100){nodes{id status supportedActions{action} lineItems(first:100){nodes{id remainingQuantity} pageInfo{hasNextPage}}} pageInfo{hasNextPage}}
        }}''',{"id":order_id})["order"]
        if not data: raise ShopifyError("ORDER_NOT_FOUND")
        return data

    def product(self, product_id):
        value = self.gql("query($id:ID!){product(id:$id){id title status tags descriptionHtml updatedAt}}",{"id":product_id})["product"]
        if not value: raise ShopifyError("PRODUCT_NOT_FOUND")
        return value

    def inventory(self, item_id, location_id):
        value = self.gql('''query($id:ID!,$location:ID!){inventoryItem(id:$id){id tracked variant{product{id tags}} inventoryLevel(locationId:$location){quantities(names:["available"]){name quantity}}}}''',{"id":item_id,"location":location_id})["inventoryItem"]
        if not value or not value.get("inventoryLevel"): raise ShopifyError("INVENTORY_NOT_ACTIVE")
        return value

    def mutation(self, query, variables, name):
        result = self.gql(query,variables,True)[name]
        self.last_validation_errors = result.get("userErrors",[])
        if result.get("userErrors"):
            # Only enum codes are exposed. Messages can contain merchant/customer supplied text.
            code = str(result["userErrors"][0].get("code") or "BUSINESS_VALIDATION")
            raise ShopifyError(code)
        return result

    def inventory_set(self, item_id, location_id, before, quantity, key):
        return self.mutation('''mutation($input:InventorySetQuantitiesInput!,$key:String!){inventorySetQuantities(input:$input) @idempotent(key:$key){inventoryAdjustmentGroup{changes{name delta quantityAfterChange}} userErrors{code field message}}}''',
            {"input":{"name":"available","reason":"correction","referenceDocumentUri":"xiaokua://actions/"+key,"quantities":[{"inventoryItemId":item_id,"locationId":location_id,"changeFromQuantity":before,"quantity":quantity}]},"key":key},"inventorySetQuantities")

    def product_update(self, product_id, description):
        return self.mutation('''mutation($product:ProductUpdateInput!){productUpdate(product:$product){product{id descriptionHtml} userErrors{field message}}}''',{"product":{"id":product_id,"descriptionHtml":description}},"productUpdate")

    def fulfill(self, order, tracking):
        groups = []
        fo = order["fulfillmentOrders"]
        if fo["pageInfo"]["hasNextPage"]: raise ShopifyError("ORDER_TOO_LARGE_REVIEW_REQUIRED")
        for f in fo["nodes"]:
            if f["lineItems"]["pageInfo"]["hasNextPage"]: raise ShopifyError("ORDER_TOO_LARGE_REVIEW_REQUIRED")
            if f["status"] not in ("OPEN","IN_PROGRESS"): continue
            items = [{"id":l["id"],"quantity":l["remainingQuantity"]} for l in f["lineItems"]["nodes"] if l["remainingQuantity"] > 0]
            if items: groups.append({"fulfillmentOrderId":f["id"],"fulfillmentOrderLineItems":items})
        if not groups: raise ShopifyError("NO_FULFILLABLE_ITEMS")
        return self.mutation('''mutation($fulfillment:FulfillmentInput!){fulfillmentCreate(fulfillment:$fulfillment){fulfillment{id status trackingInfo{number company}} userErrors{field message}}}''',
            {"fulfillment":{"lineItemsByFulfillmentOrder":groups,"notifyCustomer":False,"trackingInfo":{"number":tracking["number"],"company":tracking["company"]}}},"fulfillmentCreate")

    def tracking_update(self, fulfillment_id, number, company):
        return self.mutation('''mutation($id:ID!,$tracking:FulfillmentTrackingInput!){fulfillmentTrackingInfoUpdate(fulfillmentId:$id,trackingInfoInput:$tracking,notifyCustomer:false){fulfillment{id status trackingInfo{number company}} userErrors{field message}}}''',
            {"id":fulfillment_id,"tracking":{"number":number,"company":company}},"fulfillmentTrackingInfoUpdate")

    def cancel(self, order_id, reason):
        # Cancellation never implicitly creates a financial refund. That is a separate audited action.
        result=self.mutation('''mutation($id:ID!,$reason:OrderCancelReason!){orderCancel(orderId:$id,reason:$reason,notifyCustomer:false,refundMethod:{originalPaymentMethodsRefund:false},restock:true){job{id done} userErrors{field message} orderCancelUserErrors{code field message}}}''',{"id":order_id,"reason":reason},"orderCancel")
        if result.get("orderCancelUserErrors"):raise ShopifyError(str(result["orderCancelUserErrors"][0].get("code","CANCEL_VALIDATION")))
        for _ in range(10):
            if result.get("job",{}).get("done"):break
            time.sleep(1)
            # Poll current order instead of repeating the cancellation mutation.
            if self.order(order_id).get("cancelledAt"):
                result["job"]["done"]=True;break
        return result

    def refund(self, order_id, amount, currency, transaction_id, gateway, key):
        return self.mutation('''mutation($input:RefundInput!,$key:String!){refundCreate(input:$input) @idempotent(key:$key){refund{id transactions(first:20){nodes{id status kind}}} userErrors{field message}}}''',
            {"input":{"orderId":order_id,"currency":currency,"notify":False,"note":"小跨审批操作 "+key,"transactions":[{"orderId":order_id,"kind":"REFUND","amount":amount,"parentId":transaction_id,"gateway":gateway}]},"key":key},"refundCreate")

def for_connection(database, vault, connection_id, tenant_id):
    with database.transaction() as db:
        conn = row(db.execute(select(connections).where(connections.c.id == connection_id,connections.c.tenant_id == tenant_id,connections.c.provider == "shopify").with_for_update()).first())
        if not conn or conn["status"] != "active": raise ShopifyError("CONNECTION_INACTIVE")
        secret = vault.open(conn["secret"])
        if secret.get("expires_at",0) and secret["expires_at"] < clock()+90:
            values = {"client_id":secret["client_id"],"client_secret":secret["client_secret"]}
            if secret.get("refresh_token"): values.update(grant_type="refresh_token",refresh_token=secret["refresh_token"])
            elif secret.get("grant_type") == "client_credentials": values["grant_type"]="client_credentials"
            else: raise ShopifyError("REAUTHORIZE_REQUIRED")
            token = exchange(conn["external_id"],values)
            secret.update(token,expires_at=clock()+token.get("expires_in",86399))
            db.execute(update(connections).where(connections.c.id == connection_id).values(secret=vault.seal(secret)))
    return Shopify(conn["external_id"],secret["access_token"])

def upsert_resource(db, tenant_id, connection_id, kind, value):
    existing = db.execute(select(resources.c.id).where(resources.c.connection_id == connection_id,resources.c.kind == kind,resources.c.external_id == value["id"])).scalar()
    if existing:
        db.execute(update(resources).where(resources.c.id == existing,resources.c.tenant_id == tenant_id).values(data=value,updated_at=clock()))
    else:
        db.execute(resources.insert().values(id=uid("res"),tenant_id=tenant_id,connection_id=connection_id,kind=kind,external_id=value["id"],data=value,updated_at=clock()))
