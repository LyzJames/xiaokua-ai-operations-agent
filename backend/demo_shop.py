"""Persistent synthetic commerce adapter. It has no HTTP client or real credentials."""
import copy
import json
from decimal import Decimal
from pathlib import Path
from threading import RLock
from datetime import datetime, timezone


def money(amount):
    return {"shopMoney": {"amount": str(amount), "currencyCode": "USD"}}


class DemoShop:
    def __init__(self, path: Path):
        self.path, self.lock = path, RLock()
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"quantity": 10, "description": "<p>合成智能灯泡 · 仅用于研究演示</p>", "writes": [], "orders": []}
            for number in (1, 2):
                self.data["orders"].append({
                    "id": f"gid://shopify/Order/{number}", "name": f"#DEMO-000{number}", "test": True,
                    "tags": ["xiaokua-test", "synthetic"], "cancelledAt": None, "currencyCode": "USD",
                    "displayFinancialStatus": "PAID", "displayFulfillmentStatus": "UNFULFILLED",
                    "totalPriceSet": money("19.90"), "totalRefundedSet": money("0.00"), "netPaymentSet": money("19.90"),
                    "transactions": [{"id": f"gid://shopify/OrderTransaction/{number}", "kind": "SALE", "status": "SUCCESS",
                        "gateway": "demo", "test": True, "amountSet": money("19.90"), "parentTransaction": None}],
                    "fulfillments": [], "fulfillmentOrders": {"nodes": [], "pageInfo": {"hasNextPage": False}}, "refunds": [],
                    "createdAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
                    "lineItems": {"nodes": [{"id": f"gid://shopify/LineItem/{number}", "quantity": 1, "sku": "DEMO-BULB-01",
                        "variant": {"id": "gid://shopify/ProductVariant/1", "product": {"id": "gid://shopify/Product/1"}}}], "pageInfo": {"hasNextPage": False}},
                })
            self.save()

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def capability(self):
        return {"shop": {"name": "研究演示店铺 · 合成", "plan": {"partnerDevelopment": True}},
                "currentAppInstallation": {"accessScopes": [{"handle": "demo_offline"}]}}

    def orders(self, search=""):
        with self.lock:
            return copy.deepcopy(self.data["orders"])

    def order(self, oid):
        with self.lock:
            return copy.deepcopy(next(o for o in self.data["orders"] if o["id"] == oid))

    def product(self, pid):
        return {"id": pid, "descriptionHtml": self.data["description"], "tags": ["xiaokua-test", "synthetic"]}

    def variants(self):
        return [{"id": "gid://shopify/ProductVariant/1", "title": "暖白色", "sku": "DEMO-BULB-01", "price": "19.90",
            "inventoryQuantity": self.data["quantity"], "product": dict(self.product("gid://shopify/Product/1"), title="智能灯泡（合成）", status="DRAFT"),
            "inventoryItem": {"id": "gid://shopify/InventoryItem/1", "tracked": True, "inventoryLevels": {"nodes": [{
                "location": {"id": "gid://shopify/Location/1", "name": "演示仓库"},
                "quantities": [{"quantity": self.data["quantity"]}]}], "pageInfo": {"hasNextPage": False}}}}]

    def inventory(self, item, loc):
        return {"variant": {"product": {"tags": ["xiaokua-test", "synthetic"]}},
                "inventoryLevel": {"quantities": [{"quantity": self.data["quantity"]}]}}

    def inventory_set(self, item, loc, before, quantity, key):
        with self.lock:
            if key not in self.data["writes"]:
                if self.data["quantity"] != before:
                    raise ValueError("合成库存已改变")
                self.data["quantity"] = quantity
                self.data["writes"].append(key)
                self.save()
        return {"inventoryAdjustmentGroup": {"changes": []}, "userErrors": []}

    def product_update(self, pid, html):
        with self.lock:
            self.data["description"] = html
            self.save()
        return {"product": {"id": pid}, "userErrors": []}

    def change_order(self, oid, callback):
        with self.lock:
            value = next(o for o in self.data["orders"] if o["id"] == oid)
            result = callback(value)
            value["updatedAt"] = datetime.now(timezone.utc).isoformat()
            self.save()
            return result

    def refund(self, oid, amount, currency, parent, gateway, key):
        def change(order):
            result = {"refund": {"id": "gid://shopify/Refund/1", "transactions": {"nodes": [{"status": "SUCCESS"}]}}, "userErrors": []}
            if key not in self.data["writes"]:
                order["totalRefundedSet"] = money(Decimal(order["totalRefundedSet"]["shopMoney"]["amount"]) + Decimal(amount))
                order["netPaymentSet"] = money(Decimal(order["netPaymentSet"]["shopMoney"]["amount"]) - Decimal(amount))
                order["displayFinancialStatus"] = "PARTIALLY_REFUNDED"
                order["transactions"].append({"id": "gid://shopify/OrderTransaction/10", "kind": "REFUND", "status": "SUCCESS",
                    "test": True, "amountSet": money(amount), "parentTransaction": {"id": parent}})
                self.data["writes"].append(key)
            return result
        return self.change_order(oid, change)

    def fulfill(self, order, params):
        def change(value):
            fid = "gid://shopify/Fulfillment/1"
            value["displayFulfillmentStatus"] = "FULFILLED"
            value["fulfillments"] = [{"id": fid, "status": "SUCCESS", "trackingInfo": [params],
                "fulfillmentLineItems": {"nodes": [{"lineItem": {"id": line["id"]}, "quantity": line["quantity"]}
                    for line in value.get("lineItems", {}).get("nodes", [])], "pageInfo": {"hasNextPage": False}}}]
            return {"fulfillment": {"id": fid}, "userErrors": []}
        return self.change_order(order["id"], change)

    def tracking_update(self, fid, number, company):
        with self.lock:
            selected = next(f for o in self.data["orders"] for f in o["fulfillments"] if f["id"] == fid)
            selected["trackingInfo"] = [{"number": number, "company": company}]
            self.save()
        return {"fulfillment": {"id": fid}, "userErrors": []}

    def cancel(self, oid, reason):
        def change(value):
            value["cancelledAt"] = "2026-10-12T00:00:00Z"
            return {"job": {"id": "gid://shopify/Job/1", "done": True}, "orderCancelUserErrors": []}
        return self.change_order(oid, change)
