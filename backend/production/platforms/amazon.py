"""Amazon SP-API connector: Orders 2026, FBA inventory and guarded dynamic sandbox MCF."""
import hashlib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
import httpx
from .contracts import PlatformError, OrderSnapshot, OrderLine, Money, ProductSnapshot, InventoryLocation

ENDPOINTS = {region: {"sandbox": f"https://sandbox.sellingpartnerapi-{region}.amazon.com",
    "production": f"https://sellingpartnerapi-{region}.amazon.com"} for region in ("na", "eu", "fe")}
MARKETPLACES = {"ATVPDKIKX0DER": "na", "A2EUQ1WTGCTBG2": "na", "A1AM78C64UM0Y8": "na", "A2Q3Y263D00KWC": "na",
    "A1F83G8C2ARO7P": "eu", "A1PA6795UKMFR9": "eu", "A13V1IB3VIYZZH": "eu", "APJ6JRA9NG5V4": "eu", "A1RKKUPIHCS9HS": "eu",
    "A1VC38T7YXB528": "fe", "A39IBJ37TRP1C6": "fe", "A19VAU5U5O7RUS": "fe"}
ORDER_PATH = "/orders/2026-01-01/orders"
OUTBOUND_PATH = "/fulfillment/outbound/2026-07-04/orders"
SANDBOX_ORDER_FIXTURES = {
    "fe": {"createdAfter": "2024-12-25T00:00:00Z", "marketplaceIds": "A1VC38T7YXB528",
        "includedData": "BUYER,RECIPIENT,PROCEEDS,EXPENSE,PROMOTION,CANCELLATION,FULFILLMENT,PACKAGES"},
    "eu": {"createdAfter": "2024-12-23T00:00:00Z", "marketplaceIds": "A1F83G8C2ARO7P",
        "includedData": "BUYER,RECIPIENT,PROCEEDS,EXPENSE,PROMOTION,CANCELLATION,FULFILLMENT,PACKAGES",
        "paginationToken": "9ZhB4RmVyc2lvbiAxLjAgb2YgdGhl"},
    "na": {"createdAfter": "2024-12-20T00:00:00Z", "marketplaceIds": "A2Q3Y263D00KWC",
        "includedData": "BUYER,RECIPIENT,PROCEEDS,FULFILLMENT,TAX,PAYMENT"},
}
SANDBOX_ORDER_PARAMETERS = SANDBOX_ORDER_FIXTURES["fe"]
# Hosted static fixture matching requires these datasets. Buyer/recipient content
# is dropped immediately; production requests never request those datasets.
_TOKENS, _TOKEN_LOCK = {}, threading.RLock()


class AmazonAdapter:
    provider = "amazon"

    def __init__(self, secret, info, transport=None):
        self.secret, self.info, self.transport = dict(secret), dict(info), transport
        self.region, self.environment = info.get("region", "na"), info.get("environment", "sandbox")
        self.marketplace = info.get("marketplace_id", "ATVPDKIKX0DER")
        if self.region not in ENDPOINTS or self.environment not in ("sandbox", "production"):
            raise PlatformError("AMAZON_REGION_OR_ENVIRONMENT_INVALID")
        if MARKETPLACES.get(self.marketplace) != self.region:
            raise PlatformError("AMAZON_MARKETPLACE_REGION_MISMATCH")
        if any(not isinstance(secret.get(key), str) or not secret[key] for key in ("client_id", "client_secret", "refresh_token")):
            raise PlatformError("AMAZON_CREDENTIALS_INCOMPLETE")
        self.endpoint = ENDPOINTS[self.region][self.environment]
        self._cache_key = hashlib.sha256((secret["client_id"] + secret["client_secret"] + secret["refresh_token"]).encode()).hexdigest()

    def token(self):
        with _TOKEN_LOCK:
            cached = _TOKENS.get(self._cache_key)
            if cached and cached[1] > time.time() + 90:
                return cached[0]
            try:
                with httpx.Client(timeout=20, follow_redirects=False, transport=self.transport, trust_env=False) as client:
                    response = client.post("https://api.amazon.com/auth/o2/token", data={"grant_type": "refresh_token",
                        "client_id": self.secret["client_id"], "client_secret": self.secret["client_secret"], "refresh_token": self.secret["refresh_token"]})
                if response.status_code != 200:
                    raise PlatformError("AMAZON_LWA_AUTH_FAILED")
                data = response.json()
                token = data["access_token"]
                if not isinstance(token, str) or not token:
                    raise ValueError()
                if len(_TOKENS) >= 128:
                    _TOKENS.clear()
                _TOKENS[self._cache_key] = token, time.time() + min(int(data.get("expires_in", 3600)), 3600)
                return token
            except (httpx.HTTPError, ValueError, KeyError):
                raise PlatformError("AMAZON_LWA_UNREACHABLE") from None

    def request(self, method, path, params=None, body=None, key=None):
        if not path.startswith((ORDER_PATH, "/fba/inventory/v1/", OUTBOUND_PATH)) or "?" in path or ".." in path:
            raise PlatformError("AMAZON_PATH_NOT_ALLOWED")
        writing = method != "GET"
        headers = {"x-amz-access-token": self.token(), "user-agent": "Xiaokua/0.4.0 (Language=Python)",
            "x-amz-date": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")}
        if key:
            headers["x-amzn-idempotency-token"] = key
        for attempt in range(1 if writing else 3):
            try:
                with httpx.Client(timeout=25, follow_redirects=False, transport=self.transport, trust_env=False) as client:
                    response = client.request(method, self.endpoint + path, params=params, json=body, headers=headers)
                if not writing and (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                    time.sleep(min(attempt + 1, 2))
                    continue
                if not 200 <= response.status_code < 300:
                    # Never persist provider error bodies: they can echo credentials or PII.
                    raise PlatformError("AMAZON_HTTP_" + str(response.status_code), writing and response.status_code >= 500)
                if not response.content:
                    return {}
                data = response.json()
                if not isinstance(data, dict) or data.get("errors"):
                    raise PlatformError("AMAZON_RESPONSE_ERRORS", writing)
                return data
            except (httpx.HTTPError, ValueError):
                if writing:
                    raise PlatformError("AMAZON_TRANSPORT_UNKNOWN", True) from None
                if attempt == 2:
                    raise PlatformError("AMAZON_UNREACHABLE") from None
                time.sleep(attempt + 1)
        raise PlatformError("AMAZON_READ_RETRY_EXHAUSTED")

    def capabilities(self):
        operations = ["sync", "analyze", "chat"]
        if self.environment == "sandbox":
            operations += ["sandbox_sku", "sandbox_stock", "mcf_create", "mcf_cancel", "sandbox_status", "sandbox_package"]
        return {"provider": self.provider, "operations": operations, "environment": self.environment,
            "region": self.region, "marketplace_id": self.marketplace, "orders_version": "2026-01-01",
            "fulfillment_version": "2026-07-04",
            "orders_mode": "hosted_static" if self.environment == "sandbox" else "live",
            "static_fixture_marketplace": SANDBOX_ORDER_FIXTURES[self.region]["marketplaceIds"] if self.environment == "sandbox" else None,
            "inventory_mode": "fba_readonly", "source": "Amazon SP-API",
            "limitations": ["订单静态沙箱仅读取本区域官方固定样例页，不代表所选店铺订单或完整分页。", "FBA 仓单独管理，不能直接设置实物库存。", "当前 MCF 写入仅开放动态沙箱；上线需单独验证收件资料与商户授权。"]}

    def snapshot_orders(self, params):
        query = dict(SANDBOX_ORDER_FIXTURES[self.region]) if self.environment == "sandbox" else {
            "lastUpdatedAfter": params.get("updated_after") or (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
            "marketplaceIds": self.marketplace, "includedData": "PROCEEDS,FULFILLMENT,PACKAGES,CANCELLATION"}
        result, seen = [], set()
        for _ in range(100):
            data = self.request("GET", ORDER_PATH, params=query)
            for order in data.get("orders", []):
                result.append(self.normalize_order(order))
            if self.environment == "sandbox":
                return result  # Static nextToken is illustrative, not a dynamic continuation.
            token = data.get("pagination", {}).get("nextToken")
            if not token:
                return result
            if token in seen:
                raise PlatformError("AMAZON_PAGINATION_CYCLE")
            seen.add(token)
            query["paginationToken"] = token
        raise PlatformError("AMAZON_PAGINATION_LIMIT")

    def normalize_order(self, order):
        fulfillment = order.get("fulfillment") or {}
        status = fulfillment.get("fulfillmentStatus", "UNKNOWN")
        lines = []
        for line in order.get("orderItems", []):
            product, shipping = line.get("product") or {}, line.get("fulfillment") or {}
            sku = product.get("sellerSku", "")
            cancelled = (line.get("cancellation") or {}).get("cancelledQuantity")
            if status == "CANCELLED" and cancelled is None and shipping.get("quantityFulfilled") is not None:
                cancelled = max(0, line["quantityOrdered"] - shipping["quantityFulfilled"])
            lines.append(OrderLine(id=line["orderItemId"], variant_id="amazon:sku:" + sku if sku else None,
                product_id=product.get("asin"), sku=sku, ordered=line["quantityOrdered"], shipped=shipping.get("quantityFulfilled"), cancelled=cancelled))
        total = (order.get("proceeds") or {}).get("grandTotal") or {}
        tracking = [{"number": str(p["trackingNumber"]), "company": str(p.get("carrier", ""))}
            for p in order.get("packages", []) if p.get("trackingNumber")]
        return OrderSnapshot(provider="amazon", id=order["orderId"], reference=order["orderId"],
            environment="sandbox_static" if self.environment == "sandbox" else "production", total=Money(amount=total.get("amount"), currency=total.get("currencyCode")),
            currency=total.get("currencyCode"), fulfillment_status={"UNSHIPPED": "UNFULFILLED", "PARTIALLY_SHIPPED": "PARTIALLY_FULFILLED", "SHIPPED": "FULFILLED"}.get(status, status),
            cancelled=status == "CANCELLED", fulfillment_channel={"MERCHANT": "merchant", "AMAZON": "platform"}.get(fulfillment.get("fulfilledBy"), "unknown"),
            created_at=order.get("createdTime"), updated_at=order.get("lastUpdatedTime"), lines=lines,
            lines_complete=bool(lines) and all(line.shipped is not None and line.cancelled is not None for line in lines), tracking=tracking,
            source="Amazon Orders 2026-01-01 / " + ("hosted static sandbox" if self.environment == "sandbox" else "live")).model_dump()

    def inventory_summaries(self, sku=None):
        query = {"details": "true", "granularityType": "Marketplace", "granularityId": self.marketplace, "marketplaceIds": self.marketplace}
        if sku:
            query["sellerSkus"] = sku
        items, seen = [], set()
        for _ in range(100):
            data = self.request("GET", "/fba/inventory/v1/summaries", params=query)
            items.extend(data.get("payload", {}).get("inventorySummaries", []))
            token = data.get("pagination", {}).get("nextToken")
            if not token:
                return items
            if token in seen:
                raise PlatformError("AMAZON_PAGINATION_CYCLE")
            seen.add(token)
            query["nextToken"] = token
        raise PlatformError("AMAZON_PAGINATION_LIMIT")

    def snapshot_products(self):
        return [ProductSnapshot(provider="amazon", id="amazon:sku:" + item["sellerSku"], product_id=item.get("asin") or item["sellerSku"],
            inventory_item_id="amazon:sku:" + item["sellerSku"], sku=item["sellerSku"], title=item.get("productName") or item["sellerSku"],
            available=(item.get("inventoryDetails") or {}).get("fulfillableQuantity"),
            locations=[InventoryLocation(id="amazon:fba:" + self.marketplace, name="Amazon FBA", pool_type="platform",
                available=(item.get("inventoryDetails") or {}).get("fulfillableQuantity"))], locations_complete=True,
            environment="sandbox_dynamic" if self.environment == "sandbox" else "production", source="Amazon FBA Inventory").model_dump()
            for item in self.inventory_summaries()]

    def inventory(self, sku):
        if not isinstance(sku, str) or not sku or len(sku) > 100:
            raise PlatformError("AMAZON_SKU_INVALID")
        item = next((item for item in self.inventory_summaries(sku) if item.get("sellerSku") == sku), None)
        if not item:
            raise PlatformError("AMAZON_SKU_NOT_FOUND")
        details = item.get("inventoryDetails") or {}
        return {"sku": sku, "quantity": details.get("fulfillableQuantity"), "reserved": (details.get("reservedQuantity") or {}).get("totalReservedQuantity", 0)}

    def inventory_position(self, item_id, location_id):
        if not item_id.startswith("amazon:sku:") or location_id != "amazon:fba:" + self.marketplace:
            raise PlatformError("AMAZON_INVENTORY_LOCATION_MISMATCH")
        observed = self.inventory(item_id[len("amazon:sku:"):])
        if type(observed["quantity"]) is not int:
            raise PlatformError("AMAZON_INVENTORY_UNKNOWN")
        return {"item_id": item_id, "location_id": location_id, "quantity": observed["quantity"], "pool_type": "platform"}

    def fulfillment(self, order_id):
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", order_id):
            raise PlatformError("AMAZON_FULFILLMENT_ID_INVALID")
        return self.request("GET", OUTBOUND_PATH + "/" + quote(order_id, safe="")).get("order", {})

    def read_after_write(self, call):
        try:
            return call()
        except Exception:
            raise PlatformError("AMAZON_READBACK_UNAVAILABLE", True) from None

    def mcf_matches(self, value, identifier, p):
        if value.get("orderId") != identifier:
            return False

        try:
            return any(item.get("product", {}).get("productIdentifier", {}).get("amazonSku") == p["sku"]
                and Decimal(item.get("amount", {}).get("value", "NaN")) == Decimal(p["quantity"])
                for item in value.get("lineItems", []))
        except (InvalidOperation, TypeError):
            return False

    def mcf_summary(self, value, identifier):
        return {"order_id": identifier, "status": value.get("status"), "packages": [
            {"package_id": package.get("packageId"), "status": package.get("status")}
            for shipment in value.get("shipments", []) for package in shipment.get("packages", [])]}

    def prepare_action(self, kind, payload, test_only):
        if self.environment != "sandbox" or kind not in self.capabilities()["operations"]:
            raise PlatformError("AMAZON_OPERATION_NOT_OPEN")
        p = dict(payload)
        if kind == "sandbox_sku":
            if not isinstance(p.get("sku"), str) or not re.fullmatch(r"xiaokua-test-[A-Za-z0-9-]{1,60}", p["sku"]):
                raise PlatformError("AMAZON_SANDBOX_SKU_GUARD")
            if self.inventory_summaries(p["sku"]):
                raise PlatformError("AMAZON_SANDBOX_SKU_EXISTS")
            return {"exists": False}, p
        if kind in ("sandbox_stock", "mcf_create"):
            if type(p.get("quantity")) is not int or not 1 <= p["quantity"] <= 1000000:
                raise PlatformError("AMAZON_QUANTITY_INVALID")
            before = self.inventory(p.get("sku"))
            if type(before["quantity"]) is not int:
                raise PlatformError("AMAZON_INVENTORY_UNKNOWN")
            if kind == "mcf_create" and p["quantity"] > before["quantity"]:
                raise PlatformError("AMAZON_INVENTORY_INSUFFICIENT")
        else:
            value = self.fulfillment(p.get("order_id"))
            before = {"status": value.get("status")}
            if kind == "sandbox_package":
                if not isinstance(p.get("package_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", p["package_id"]):
                    raise PlatformError("AMAZON_SANDBOX_PACKAGE_ID_INVALID")
                package = next((v for v in self.mcf_summary(value, p["order_id"])["packages"] if v["package_id"] == p["package_id"]), None)
                transitions = {"PROCESSING": {"IN_TRANSIT"}, "SHIPPED": {"IN_TRANSIT"}, "IN_TRANSIT": {"OUT_FOR_DELIVERY", "DELAYED"},
                    "DELAYED": {"IN_TRANSIT", "OUT_FOR_DELIVERY"}, "OUT_FOR_DELIVERY": {"DELIVERED", "UNDELIVERABLE"}}
                if not package or p.get("status") not in transitions.get(package["status"], set()):
                    raise PlatformError("AMAZON_SANDBOX_PACKAGE_TRANSITION_INVALID")
                return dict(before, package_status=package["status"]), p
            if kind == "mcf_cancel" and before["status"] != "PROCESSING":
                raise PlatformError("AMAZON_FULFILLMENT_NOT_CANCELLABLE")
            if kind == "sandbox_status":
                if p.get("status") not in ("PROCESSING", "COMPLETE", "COMPLETE_PARTIAL", "UNFULFILLABLE"):
                    raise PlatformError("AMAZON_SANDBOX_STATUS_INVALID")
                if before["status"] != "PROCESSING":
                    raise PlatformError("AMAZON_SANDBOX_ORDER_TERMINAL_OR_UNKNOWN")
        return before, p

    def execute_action(self, kind, p, before, key, test_only):
        observed, _ = self.prepare_action(kind, p, test_only)
        if observed != before:
            raise PlatformError("AMAZON_STATE_CHANGED")
        if kind == "sandbox_sku":
            self.create_sandbox_sku(p["sku"], key)
            after = self.read_after_write(lambda: self.inventory(p["sku"]))
            return {"sku": p["sku"], "observed": after, "source": "Amazon dynamic sandbox"}
        if kind == "sandbox_stock":
            self.request("POST", "/fba/inventory/v1/items/inventory", body={"inventoryItems": [{"sellerSku": p["sku"],
                "marketplaceId": self.marketplace, "quantity": p["quantity"]}]}, key=key)
            after = self.read_after_write(lambda: self.inventory(p["sku"]))
            matched = after["quantity"] == before["quantity"] + p["quantity"]
            return {"observed": after, "pending_verification": not matched, "expected_quantity": before["quantity"] + p["quantity"], "source": "Amazon dynamic sandbox"}
        identifier = "xk-" + hashlib.sha256(key.encode()).hexdigest()[:32] if kind == "mcf_create" else p["order_id"]
        if kind == "mcf_create":
            # Sandbox-only synthetic destination. No customer address enters a model or audit.
            body = {"orderId": identifier, "channel": "XIAOKUA", "origin": {"countryCode": "US"},
                "fulfillmentConfiguration": {"serviceLevel": {"serviceTiers": ["STANDARD"]}, "action": "SHIP", "policy": "FILL_OR_KILL",
                    "services": {"packaging": {"packagingOption": "UNBRANDED"}}},
                "destination": {"deliveryAddress": {"name": "Synthetic Buyer", "addressLine1": "123 Test Street", "city": "Seattle", "stateOrRegion": "WA", "postalCode": "98101", "countryCode": "US"}},
                "lineItems": [{"lineItemId": "item-1", "product": {"productIdentifier": {"amazonSku": p["sku"]}}, "amount": {"unit": "EACHES", "value": str(p["quantity"])}}]}
            self.request("POST", OUTBOUND_PATH, body=body, key=key)
        elif kind == "mcf_cancel":
            self.request("PUT", OUTBOUND_PATH + "/" + identifier + "/cancel", key=key)
        elif kind == "sandbox_package":
            self.request("PUT", OUTBOUND_PATH + "/" + identifier + "/packages/" + quote(p["package_id"], safe=""), body={"status": p["status"]}, key=key)
        else:
            self.request("PUT", OUTBOUND_PATH + "/" + identifier + "/status", body={"status": p["status"]}, key=key)
        order = self.read_after_write(lambda: self.fulfillment(identifier))
        summary = self.mcf_summary(order, identifier)
        if kind == "sandbox_package":
            matched = any(v["package_id"] == p["package_id"] and v["status"] == p["status"] for v in summary["packages"])
        else:
            matched = self.mcf_matches(order, identifier, p) if kind == "mcf_create" else order.get("status") == ("CANCELLED" if kind == "mcf_cancel" else p["status"])
        return dict(summary, pending_verification=not matched, source="Amazon dynamic sandbox / Fulfillment Outbound 2026-07-04")

    def verify_action(self, kind, p):
        if kind == "sandbox_sku":
            observed = self.inventory(p["sku"])
            return observed["sku"] == p["sku"], observed
        if kind == "sandbox_stock":
            observed = self.inventory(p["sku"])
            return observed["quantity"] == p.get("_expected_quantity"), observed
        identifier = "xk-" + hashlib.sha256(p["_action_id"].encode()).hexdigest()[:32] if kind == "mcf_create" else p["order_id"]
        order = self.fulfillment(identifier)
        summary = self.mcf_summary(order, identifier)
        if kind == "sandbox_package":
            matched = any(v["package_id"] == p["package_id"] and v["status"] == p["status"] for v in summary["packages"])
        elif kind == "mcf_create":
            matched = self.mcf_matches(order, identifier, p)
        else:
            matched = order.get("status") == ("CANCELLED" if kind == "mcf_cancel" else p["status"])
        return matched, summary

    def create_sandbox_sku(self, sku, key=None):
        if self.environment != "sandbox" or not re.fullmatch(r"xiaokua-test-[A-Za-z0-9-]{1,60}", sku):
            raise PlatformError("AMAZON_SANDBOX_SKU_GUARD")
        return self.request("POST", "/fba/inventory/v1/items", body={"sellerSku": sku, "marketplaceId": self.marketplace, "productName": "Synthetic acceptance item"}, key=key)
