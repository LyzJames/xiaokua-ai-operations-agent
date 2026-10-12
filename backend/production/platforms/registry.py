"""Only registered, authorized commerce connections can obtain adapters."""
from ..database import connections
from .contracts import PlatformError, PROVIDERS, provider_spec, shopify_operations, CommerceAdapter
from .shopify import ShopifyAdapter
from .amazon import AmazonAdapter


def build_shopify(db, vault, connection):
    from ..shopify import for_connection
    native = for_connection(db, vault, connection["id"], connection["tenant_id"])
    return ShopifyAdapter(native, connection)


ADAPTER_BUILDERS = {"shopify": build_shopify,
    "amazon": lambda db, vault, connection: AmazonAdapter(vault.open(connection["secret"]), connection["info"])}


def connection_adapter(db, vault, connection_id, tenant_id, client_factory=None):
    connection = db.get(connections, connection_id, tenant_id)
    if not connection or connection["status"] != "active":
        raise PlatformError("COMMERCE_CONNECTION_INACTIVE_OR_FOREIGN")
    provider = provider_spec(connection["provider"])
    if provider.stage == "contract_only" and not client_factory:
        raise PlatformError("PROVIDER_NOT_INTEGRATED")
    if client_factory:
        native = client_factory(connection_id, tenant_id)
        if hasattr(native, "snapshot_orders") or hasattr(native, "prepare_action"):
            if not isinstance(native, CommerceAdapter):
                raise PlatformError("ADAPTER_CONTRACT_INCOMPLETE")
            if getattr(native, "provider", None) != connection["provider"]:
                raise PlatformError("ADAPTER_PROVIDER_MISMATCH")
            return native
    else:
        native = None
    if provider.key == "shopify":
        if native is not None:
            return ShopifyAdapter(native, connection)
    if native is not None:
        raise PlatformError("ADAPTER_CONTRACT_INCOMPLETE")
    builder = ADAPTER_BUILDERS.get(provider.key)
    if not builder:
        raise PlatformError("PROVIDER_NOT_INTEGRATED")
    return builder(db, vault, connection)


def connection_capabilities(connection):
    provider = PROVIDERS.get(connection["provider"])
    if not provider:
        return None
    operations = set(provider.operations) if connection["status"] == "active" else set()
    if provider.key == "shopify" and connection["status"] == "active":
        operations = shopify_operations(connection["info"])
    if provider.key == "amazon" and connection["info"].get("environment") != "sandbox":
        operations &= {"sync", "analyze", "chat"}
    return {"provider": provider.key, "name": provider.name, "stage": provider.stage,
            "operations": sorted(operations), "environment": connection["info"].get("environment", "demo" if connection["info"].get("demo") else "production_or_test"),
            "inventory_allocation": "inventory" in operations,
            "qualification_required": provider.stage == "contract_only"}
