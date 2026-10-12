"""Shopify business semantics behind the common commerce action contract."""
import html
from decimal import Decimal
from .contracts import PlatformError, shopify_order, shopify_product, shopify_operations


class ShopifyAdapter:
    provider = "shopify"

    def __init__(self, native, connection):
        self.native, self.connection = native, connection

    def __getattr__(self, name):
        return getattr(self.native, name)

    def capabilities(self):
        return {"provider": self.provider, "operations": sorted(shopify_operations(self.connection["info"])),
                "environment": "demo" if self.connection["info"].get("demo") else "production_or_test",
                "inventory_mode": "compare_and_set", "source": "Shopify Admin API"}

    def snapshot_orders(self, params):
        return [shopify_order(value, self.connection["info"].get("demo", False)) for value in self.native.orders(params.get("search", ""))]

    def snapshot_products(self):
        return [shopify_product(value, self.connection["info"].get("demo", False)) for value in self.native.variants()]

    def inventory_position(self, item_id, location_id):
        value = self.native.inventory(item_id, location_id)
        quantities = value.get("inventoryLevel", {}).get("quantities", [])
        quantity = next((q["quantity"] for q in quantities if q.get("name", "available") == "available"), None)
        if type(quantity) is not int:
            raise PlatformError("SHOPIFY_INVENTORY_UNKNOWN")
        return {"item_id": item_id, "location_id": location_id, "quantity": quantity, "pool_type": "merchant"}

    def prepare_action(self, kind, payload, test_only):
        from ..workflows import BusinessError, valid_gid, cents, order_snapshot, check_test_order, refund_transaction
        p, before = dict(payload), {}
        if kind in ("fulfill", "tracking", "cancel", "refund"):
            order = self.native.order(valid_gid(p.get("order_id"), "Order"))
            check_test_order(order, test_only)
            if order.get("cancelledAt"):
                raise BusinessError("订单已取消")
            before = order_snapshot(order)
            if kind in ("fulfill", "tracking"):
                for field in ("number", "company"):
                    if not isinstance(p.get(field), str) or not p[field].strip() or len(p[field]) > 100:
                        raise BusinessError("请提供物流公司与真实物流单号")
            if kind == "tracking":
                fid = valid_gid(p.get("fulfillment_id"), "Fulfillment")
                selected = next((f for f in order["fulfillments"] if f["id"] == fid and f["status"] != "CANCELLED"), None)
                if not selected:
                    raise BusinessError("该履约单不属于订单或已取消")
                before = dict(before, tracking=selected.get("trackingInfo", []))
            if kind == "cancel" and p.get("reason", "OTHER") not in ("CUSTOMER", "FRAUD", "INVENTORY", "DECLINED", "OTHER"):
                raise BusinessError("取消原因无效")
            if kind == "refund":
                amount = cents(p.get("amount"))
                currency = order["currencyCode"]
                if p.get("currency", currency) != currency:
                    raise BusinessError("退款币种须与订单币种一致")
                parent = refund_transaction(order, amount, test_only)
                p.update(amount_cents=amount, currency=currency, parent_id=parent["id"], gateway=parent["gateway"])
        elif kind == "inventory":
            item, location = valid_gid(p.get("item_id"), "InventoryItem"), valid_gid(p.get("location_id"), "Location")
            value = self.native.inventory(item, location)
            if test_only and "xiaokua-test" not in value["variant"]["product"]["tags"]:
                raise BusinessError("测试模式仅允许带 xiaokua-test 标签的商品")
            if type(p.get("quantity")) is not int or not 0 <= p["quantity"] <= 1000000:
                raise BusinessError("库存须为 0–1000000 的整数")
            before = {"quantity": value["inventoryLevel"]["quantities"][0]["quantity"]}
        elif kind == "product":
            value = self.native.product(valid_gid(p.get("product_id"), "Product"))
            if test_only and "xiaokua-test" not in value["tags"]:
                raise BusinessError("测试模式仅允许带 xiaokua-test 标签的商品")
            if not isinstance(p.get("description"), str) or not p["description"].strip() or len(p["description"]) > 15000:
                raise BusinessError("产品说明须为 1–15000 字")
            p["description_html"] = "<p>" + html.escape(p["description"]).replace("\n", "</p><p>") + "</p>"
            before = {"description_html": value["descriptionHtml"]}
        else:
            raise PlatformError("SHOPIFY_OPERATION_UNSUPPORTED")
        return before, p

    def execute_action(self, kind, payload, before, key, test_only):
        from ..workflows import BusinessError
        p = payload
        observed, prepared = self.prepare_action(kind, p, test_only)
        if observed != before:
            raise BusinessError("平台业务状态已变化，请重新生成并审批方案")
        if kind == "refund":
            if prepared["parent_id"] != p["parent_id"]:
                raise BusinessError("退款来源交易已改变")
            result = self.native.refund(p["order_id"], str(Decimal(p["amount_cents"]) / 100), p["currency"], p["parent_id"], p["gateway"], key)
            statuses = [t["status"] for t in result["refund"]["transactions"]["nodes"]]
            if not statuses or any(status != "SUCCESS" for status in statuses):
                raise PlatformError("REFUND_TRANSACTION_NOT_SUCCESS", True)
            return result
        if kind == "fulfill":
            result = self.native.fulfill(self.native.order(p["order_id"]), {"number": p["number"], "company": p["company"]})
            p = dict(p, fulfillment_id=result.get("fulfillment", {}).get("id"))
        elif kind == "tracking":
            result = self.native.tracking_update(p["fulfillment_id"], p["number"], p["company"])
        elif kind == "cancel":
            result = self.native.cancel(p["order_id"], p.get("reason", "OTHER"))
            if result.get("orderCancelUserErrors"):
                raise PlatformError("CANCEL_VALIDATION")
        elif kind == "inventory":
            result = self.native.inventory_set(p["item_id"], p["location_id"], before["quantity"], p["quantity"], key)
        else:
            result = self.native.product_update(p["product_id"], p["description_html"])
        try:
            matched, proof = self.verify_action(kind, p)
        except Exception:
            raise PlatformError("SHOPIFY_READBACK_UNAVAILABLE", True) from None
        return dict(result, observed=proof, pending_verification=not matched)

    def verify_action(self, kind, p):
        from ..workflows import cents_nonnegative
        if kind in ("refund", "cancel", "fulfill", "tracking"):
            value = self.native.order(p["order_id"])
            if kind == "cancel":
                return bool(value.get("cancelledAt")), {"cancelled_at": value.get("cancelledAt")}
            if kind in ("fulfill", "tracking"):
                matched = any(i.get("number") == p["number"] and i.get("company") == p["company"] for f in value.get("fulfillments", [])
                    if f.get("status") == "SUCCESS" and (kind == "fulfill" and not p.get("fulfillment_id") or f["id"] == p.get("fulfillment_id")) for i in f.get("trackingInfo", []))
                return matched, {"fulfillment_ids": [f["id"] for f in value.get("fulfillments", [])]}
            matched = [r for r in value.get("refunds", []) if r.get("note") == "小跨审批操作 " + p.get("_action_id", "")]
            result = any(sum(cents_nonnegative(t["amountSet"]["shopMoney"]["amount"]) for t in r["transactions"]["nodes"] if t["status"] == "SUCCESS") == p["amount_cents"] for r in matched)
            return result, {"refund_ids": [r["id"] for r in matched]}
        if kind == "inventory":
            quantity = self.inventory_position(p["item_id"], p["location_id"])["quantity"]
            return quantity == p["quantity"], {"quantity": quantity}
        value = self.native.product(p["product_id"])
        return value["descriptionHtml"] == p["description_html"], {"product_id": value["id"], "description_matches": value["descriptionHtml"] == p["description_html"]}
