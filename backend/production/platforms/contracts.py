"""Provider-neutral snapshots and capability contracts, without customer identity data."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol, runtime_checkable
from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlatformError(RuntimeError):
    def __init__(self, code: str, unknown: bool = False):
        self.code, self.unknown = code, unknown
        super().__init__(code)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Money(Contract):
    amount: str | None = None
    currency: str | None = None

    @field_validator("amount")
    @classmethod
    def exact_amount(cls, value):
        if value is None:
            return value
        try:
            number = Decimal(value)
            if not number.is_finite() or number < 0:
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise ValueError("Invalid provider money") from None
        return value


class OrderLine(Contract):
    id: str
    variant_id: str | None = None
    product_id: str | None = None
    sku: str = ""
    ordered: int = Field(ge=0)
    shipped: int | None = Field(default=None, ge=0)
    cancelled: int | None = Field(default=None, ge=0)


class OrderSnapshot(Contract):
    schema_version: int = 1
    provider: str
    id: str
    reference: str
    environment: Literal["production", "test", "sandbox_static", "sandbox_dynamic", "demo"]
    currency: str | None = None
    total: Money = Field(default_factory=Money)
    refunded: Money = Field(default_factory=Money)
    financial_status: str = "UNKNOWN"
    fulfillment_status: str = "UNKNOWN"
    cancelled: bool = False
    fulfillment_channel: Literal["merchant", "platform", "unknown"] = "unknown"
    created_at: str | None = None
    updated_at: str | None = None
    lines: list[OrderLine] = Field(default_factory=list)
    lines_complete: bool = False
    tracking: list[dict[str, str]] = Field(default_factory=list)
    source: str


class InventoryLocation(Contract):
    id: str
    name: str
    available: int | None = None
    pool_type: Literal["merchant", "platform"] = "merchant"


class ProductSnapshot(Contract):
    schema_version: int = 1
    provider: str
    id: str
    product_id: str
    inventory_item_id: str
    sku: str
    title: str
    status: str = "UNKNOWN"
    price: Money = Field(default_factory=Money)
    description: str = ""
    available: int | None = None
    locations: list[InventoryLocation] = Field(default_factory=list)
    locations_complete: bool = False
    environment: Literal["production", "test", "sandbox_dynamic", "demo"]
    source: str


@dataclass(frozen=True)
class ProviderSpec:
    key: str
    name: str
    stage: str
    operations: frozenset[str]
    authorization: str
    documentation: str

    def public(self):
        return {"provider": self.key, "name": self.name, "stage": self.stage,
                "operations": sorted(self.operations), "authorization": self.authorization,
                "documentation": self.documentation}


PROVIDERS = {
    "shopify": ProviderSpec("shopify", "Shopify", "connected_adapter",
        frozenset({"sync", "analyze", "chat", "inventory", "product", "fulfill", "tracking", "cancel", "refund"}),
        "merchant_oauth", "https://shopify.dev/docs/api/admin-graphql/2026-10"),
    "amazon": ProviderSpec("amazon", "Amazon", "connected_adapter",
        frozenset({"sync", "analyze", "chat", "mcf_create", "mcf_cancel", "sandbox_sku", "sandbox_stock", "sandbox_status", "sandbox_package"}),
        "selling_partner_lwa", "https://developer-docs.amazon/sp-api/docs/connecting-to-the-selling-partner-api"),
    "taobao": ProviderSpec("taobao", "淘宝", "contract_only", frozenset(), "merchant_authorization",
        "https://open.taobao.com/"),
    "jd": ProviderSpec("jd", "京东", "contract_only", frozenset(), "merchant_authorization",
        "https://open.jd.com/"),
    "pinduoduo": ProviderSpec("pinduoduo", "拼多多", "contract_only", frozenset(), "merchant_authorization",
        "https://open.pinduoduo.com/"),
    "temu": ProviderSpec("temu", "TEMU", "contract_only", frozenset(), "merchant_authorization",
        "https://partner.temu.com/"),
}


@runtime_checkable
class CommerceAdapter(Protocol):
    provider: str
    def capabilities(self) -> dict: ...
    def snapshot_orders(self, params: dict) -> list[dict]: ...
    def snapshot_products(self) -> list[dict]: ...
    def inventory_position(self, item_id: str, location_id: str) -> dict: ...
    def prepare_action(self, kind: str, payload: dict, test_only: bool) -> tuple[dict, dict]: ...
    def execute_action(self, kind: str, payload: dict, before: dict, key: str, test_only: bool) -> dict: ...
    def verify_action(self, kind: str, payload: dict) -> tuple[bool, dict]: ...


def provider_spec(key):
    try:
        return PROVIDERS[key]
    except KeyError:
        raise PlatformError("UNKNOWN_COMMERCE_PROVIDER") from None


def shopify_operations(info):
    operations = set(PROVIDERS["shopify"].operations)
    if info.get("demo") or "scopes" not in info:
        return operations
    grants = set(info["scopes"])
    requirements = {"inventory": "write_inventory", "product": "write_products", "cancel": "write_orders",
        "refund": "write_orders", "fulfill": "write_merchant_managed_fulfillment_orders", "tracking": "write_merchant_managed_fulfillment_orders"}
    return operations - {kind for kind, scope in requirements.items() if scope not in grants}


def shopify_order(value, demo=False):
    fulfilled = {}
    complete = value.get("lineItems", {}).get("pageInfo", {}).get("hasNextPage") is False
    for fulfillment in value.get("fulfillments", []):
        if fulfillment.get("status") != "SUCCESS":
            continue
        lines = fulfillment.get("fulfillmentLineItems", {})
        if lines.get("pageInfo", {}).get("hasNextPage") is not False:
            complete = False
        for line in lines.get("nodes", []):
            identifier = (line.get("lineItem") or {}).get("id")
            if identifier and type(line.get("quantity")) is int:
                fulfilled[identifier] = fulfilled.get(identifier, 0) + line["quantity"]
    lines = []
    for index, line in enumerate(value.get("lineItems", {}).get("nodes", [])):
        variant = line.get("variant") or {}
        identifier = line.get("id") or f"{value['id']}:line:{index}"
        quantity = line.get("quantity")
        if type(quantity) is not int or quantity < 0:
            complete = False
            continue
        shipped = fulfilled.get(identifier, 0)
        lines.append(OrderLine(id=identifier, variant_id=variant.get("id"), product_id=(variant.get("product") or {}).get("id"),
            sku=line.get("sku") or "", ordered=quantity, shipped=shipped,
            cancelled=quantity-shipped if value.get("cancelledAt") else 0))
    def money(field):
        source = value.get(field, {}).get("shopMoney") or {}
        return Money(amount=source.get("amount"), currency=source.get("currencyCode"))
    return OrderSnapshot(provider="shopify", id=value["id"], reference=value.get("name", value["id"]),
        environment="demo" if demo else "test" if value.get("test") else "production",
        currency=value.get("currencyCode"), total=money("totalPriceSet"), refunded=money("totalRefundedSet"),
        financial_status=value.get("displayFinancialStatus", "UNKNOWN"), fulfillment_status=value.get("displayFulfillmentStatus", "UNKNOWN"),
        cancelled=bool(value.get("cancelledAt")), fulfillment_channel="merchant", created_at=value.get("createdAt"), updated_at=value.get("updatedAt"),
        lines=lines, lines_complete=complete and bool(lines), tracking=[{k: str(info[k]) for k in ("number", "company") if info.get(k)}
            for fulfillment in value.get("fulfillments", []) for info in fulfillment.get("trackingInfo", [])], source="Shopify Admin API").model_dump()


def shopify_product(value, demo=False):
    product = value.get("product") or {}
    item = value.get("inventoryItem") or {}
    locations = item.get("inventoryLevels") or {}
    import html, re
    return ProductSnapshot(provider="shopify", id=value["id"], product_id=product.get("id") or value["id"],
        inventory_item_id=item.get("id") or value["id"], sku=value.get("sku") or "", title=product.get("title") or value.get("title") or "未命名商品",
        status=product.get("status", "UNKNOWN"), price=Money(amount=value.get("price")),
        description=html.unescape(re.sub(r"<[^>]+>", " ", product.get("descriptionHtml", ""))),
        available=value["inventoryQuantity"] if type(value.get("inventoryQuantity")) is int else None,
        locations=[InventoryLocation(id=level["location"]["id"], name=level["location"].get("name", "库存地点"),
            available=next((q["quantity"] for q in level.get("quantities", []) if q.get("name", "available") == "available"), None))
            for level in locations.get("nodes", [])], locations_complete=locations.get("pageInfo", {}).get("hasNextPage") is False,
        environment="demo" if demo else "test" if "xiaokua-test" in product.get("tags", []) else "production", source="Shopify Admin API").model_dump()


def normalize_resource(provider, kind, value, demo=False):
    if provider == "shopify":
        return shopify_order(value, demo) if kind == "order" else shopify_product(value, demo)
    model = OrderSnapshot if kind == "order" else ProductSnapshot
    snapshot = model.model_validate(value)
    if snapshot.provider != provider:
        raise PlatformError("SNAPSHOT_PROVIDER_MISMATCH")
    return snapshot.model_dump()
