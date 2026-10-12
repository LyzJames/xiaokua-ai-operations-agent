"""Tenant-scoped physical stock ledger and decrease-before-increase allocation plans."""
from datetime import datetime, timezone
from sqlalchemy import select, update, func
from .database import (inventory_pools, sku_mappings, inventory_positions, stock_journal, stock_plans,
    connections, resources, tasks, actions, tenants, clock, uid, row, canonical)
from .security import digest
from .platforms.contracts import normalize_resource
from .platforms.registry import connection_capabilities


def source_time(value):
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return 0.0
        return parsed.timestamp()
    except (ValueError, TypeError):
        return 0.0


class SharedInventory:
    def __init__(self, db, ops):
        self.db, self.ops = db, ops

    def error(self, message):
        from .workflows import BusinessError
        raise BusinessError(message)

    def lock_tenant(self, c, t):
        c.execute(select(tenants.c.id).where(tenants.c.id == t).with_for_update()).first()

    def pool(self, c, t, identifier):
        value = row(c.execute(select(inventory_pools).where(inventory_pools.c.id == identifier,
            inventory_pools.c.tenant_id == t).with_for_update()).first())
        if not value:
            self.error("共享库存池不存在")
        return value

    def reserved(self, c, t, pool_id):
        return c.execute(select(func.coalesce(func.sum(inventory_positions.c.reserved), 0)).where(
            inventory_positions.c.tenant_id == t, inventory_positions.c.pool_id == pool_id)).scalar()

    def state(self, t):
        pools = self.db.list(inventory_pools, t, 500)
        with self.db.engine.connect() as c:
            for pool in pools:
                pool["reserved"] = self.reserved(c, t, pool["id"])
                pool["available"] = max(0, pool["on_hand"] - pool["reserved"] - pool["safety"])
        return {"pools": pools, "mappings": self.db.list(sku_mappings, t, 1000),
            "positions": self.db.list(inventory_positions, t, 500), "journal": self.db.list(stock_journal, t, 100),
            "plans": self.db.list(stock_plans, t, 100)}

    def create_pool(self, actor, name, sku, warehouse, safety=5, pool_type="merchant"):
        name, sku, warehouse = name.strip(), sku.strip(), warehouse.strip()
        if not name or not sku or not warehouse:
            self.error("池名称、主 SKU 和仓库不能为空")
        if pool_type not in ("merchant", "platform") or type(safety) is not int or not 0 <= safety <= 1000000:
            self.error("仓库类型或安全库存无效")
        value = dict(id=uid("pool"), tenant_id=actor["tenant_id"], name=name, sku=sku, warehouse=warehouse,
            pool_type=pool_type, on_hand=0, safety=safety, version=1, initialized=False, frozen=False, issues=[], created_at=clock())
        with self.db.transaction() as c:
            self.lock_tenant(c, actor["tenant_id"])
            c.execute(inventory_pools.insert().values(**value))
            self.db.audit(c, actor["tenant_id"], actor["id"], "stock.pool.created", value["id"], {"sku": sku, "warehouse": warehouse, "pool_type": pool_type})
        return value

    def add_mapping(self, actor, pool_id, connection_id, variant_id, location_id):
        t = actor["tenant_id"]
        connection = self.db.get(connections, connection_id, t)
        value = self.db.list(resources, t, 1, connection_id=connection_id, kind="variant", external_id=variant_id)
        if not connection or connection["status"] != "active" or not value:
            self.error("请选择本组织已同步的店铺规格")
        snapshot = normalize_resource(connection["provider"], "variant", value[0]["data"], connection["info"].get("demo", False))
        location = next((v for v in snapshot["locations"] if v["id"] == location_id), None)
        if not snapshot["locations_complete"] or not location or type(location["available"]) is not int:
            self.error("库存地点资料不完整，请先同步")
        initial_pool = self.db.get(inventory_pools, pool_id, t)
        if not initial_pool:
            self.error("共享库存池不存在")
        if initial_pool["pool_type"] != location["pool_type"]:
            self.error("FBA 平台仓与商家自发货仓必须分别建池")
        # Native credential readers may open their own transaction to refresh a token.
        # Resolve them before acquiring the stock transaction, including on SQLite.
        allocation = initial_pool["pool_type"] != "merchant" or "inventory" in self.ops.client(connection_id, t).capabilities()["operations"]
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            current_connection = self.db.get(connections, connection_id, t, c)
            current_resource = self.db.get(resources, value[0]["id"], t, c)
            if not current_connection or current_connection["status"] != "active" or not current_resource or current_resource["updated_at"] != value[0]["updated_at"]:
                self.error("店铺授权或规格快照已变化，请重新同步后映射")
            pool = self.pool(c, t, pool_id)
            if pool["pool_type"] != location["pool_type"]:
                self.error("FBA 平台仓与商家自发货仓必须分别建池")
            if pool["pool_type"] == "merchant" and not allocation:
                self.error("该适配器尚未开放共享库存分配写入")
            if pool["pool_type"] == "platform" and c.execute(select(sku_mappings.c.id).where(
                sku_mappings.c.tenant_id == t, sku_mappings.c.pool_id == pool_id)).first():
                self.error("每个平台仓池只映射一个店铺规格，避免重复计算平台库存")
            if c.execute(select(stock_plans.c.id).where(stock_plans.c.tenant_id == t, stock_plans.c.pool_id == pool_id,
                stock_plans.c.status.in_(["pending", "running", "needs_review"]))).first():
                self.error("先核实并结案旧分配方案，再改变仓库映射")
            if c.execute(select(sku_mappings.c.id).where(sku_mappings.c.tenant_id == t,
                sku_mappings.c.connection_id == connection_id, sku_mappings.c.variant_id == variant_id)).first():
                self.error("该店铺规格已有仓库映射；首版要求每个规格对应一个明确仓库")
            mapping = dict(id=uid("mapping"), tenant_id=t, pool_id=pool_id, connection_id=connection_id,
                variant_id=variant_id, item_id=snapshot["inventory_item_id"], location_id=location_id, quota=location["available"], created_at=clock())
            c.execute(sku_mappings.insert().values(**mapping))
            c.execute(update(inventory_pools).where(inventory_pools.c.id == pool_id).values(version=pool["version"] + 1))
            self.db.audit(c, t, actor["id"], "stock.mapping.created", mapping["id"], {"pool_id": pool_id, "connection_id": connection_id, "variant_id": variant_id, "location_id": location_id})
        self.ingest(connection_id, t)
        return mapping

    def freeze(self, c, pool, code):
        issues = list(dict.fromkeys(pool["issues"] + [code]))
        if pool["frozen"] and issues == pool["issues"]:
            return
        c.execute(update(inventory_pools).where(inventory_pools.c.id == pool["id"], inventory_pools.c.tenant_id == pool["tenant_id"]).values(frozen=True, issues=issues))
        self.db.audit(c, pool["tenant_id"], "inventory", "stock.frozen", pool["id"], {"code": code})
        pool["frozen"], pool["issues"] = True, issues

    def disconnect(self, t, connection_id):
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            for mapping in self.db.list(sku_mappings, t, 1000, connection_id=connection_id):
                self.freeze(c, self.pool(c, t, mapping["pool_id"]), "CONNECTION_REVOKED")

    def source_ready(self, connection, pool_type="merchant"):
        if not connection or connection["status"] != "active":
            return False
        info = connection["info"]
        errors = info.get("sync_errors", [])
        if pool_type == "platform":
            errors = [v for v in errors if v.get("component") != "orders"]
        return not errors and (pool_type == "platform" or info.get("order_access", False)) and clock()-info.get("last_sync", 0) <= 300

    def ingest(self, connection_id, t):
        mappings = self.db.list(sku_mappings, t, 1000, connection_id=connection_id)
        if not mappings:
            return []
        connection = self.db.get(connections, connection_id, t)
        orders = self.db.list(resources, t, 10000, connection_id=connection_id, kind="order")
        normalized = [normalize_resource(connection["provider"], "order", value["data"], connection["info"].get("demo", False)) for value in orders]
        by_variant = {mapping["variant_id"]: mapping for mapping in mappings}
        issues = []
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            for mapping in mappings:
                pool = self.pool(c, t, mapping["pool_id"])
                if not self.source_ready(connection, pool["pool_type"]):
                    self.freeze(c, pool, "STORE_SYNC_INCOMPLETE")
                    issues.append({"pool_id": pool["id"], "code": "STORE_SYNC_INCOMPLETE"})
                product = self.db.list(resources, t, 1, connection_id=connection_id, kind="variant", external_id=mapping["variant_id"])
                quantity = None
                if product:
                    value = normalize_resource(connection["provider"], "variant", product[0]["data"])
                    quantity = next((loc["available"] for loc in value["locations"] if loc["id"] == mapping["location_id"]), None)
                if type(quantity) is int:
                    c.execute(update(sku_mappings).where(sku_mappings.c.id == mapping["id"], sku_mappings.c.tenant_id == t).values(quota=max(0, quantity)))
                    if quantity < 0:
                        self.freeze(c, pool, "PLATFORM_NEGATIVE_AVAILABLE")
                    if pool["pool_type"] == "platform":
                            c.execute(update(inventory_pools).where(inventory_pools.c.id == pool["id"]).values(on_hand=max(0, quantity), initialized=True, version=pool["version"] + (pool["on_hand"] != quantity)))
                else:
                    self.freeze(c, pool, "MAPPED_INVENTORY_UNKNOWN")
            for order in normalized:
                if order["environment"] != "sandbox_static" and order["fulfillment_channel"] == "unknown":
                    for identifier in {m["pool_id"] for m in mappings}:
                        pool = self.pool(c, t, identifier)
                        if pool["pool_type"] == "merchant":
                            self.freeze(c, pool, "FULFILLMENT_CHANNEL_UNKNOWN")
                    continue
                if order["environment"] == "sandbox_static" or order["fulfillment_channel"] != "merchant":
                    continue
                if not source_time(order["created_at"]) or not source_time(order["updated_at"]):
                    for identifier in {m["pool_id"] for m in mappings}:
                        pool = self.pool(c, t, identifier)
                        if pool["pool_type"] == "merchant":
                            self.freeze(c, pool, "ORDER_TIME_UNKNOWN")
                    continue
                old_order = list(c.execute(select(inventory_positions).where(inventory_positions.c.tenant_id == t,
                    inventory_positions.c.connection_id == connection_id, inventory_positions.c.order_id == order["id"])))
                affected = {mapping["pool_id"] for mapping in mappings} if not order["lines_complete"] else {
                    by_variant[line["variant_id"]]["pool_id"] for line in order["lines"] if line["variant_id"] in by_variant}
                if not order["lines_complete"]:
                    if order["cancelled"] or order["fulfillment_status"] == "FULFILLED":
                        if not old_order:
                            continue
                    for pool_id in affected:
                        pool = self.pool(c, t, pool_id)
                        self.freeze(c, pool, "ORDER_LINES_INCOMPLETE")
                        issues.append({"pool_id": pool_id, "code": "ORDER_LINES_INCOMPLETE"})
                    continue
                present = {line["id"] for line in order["lines"]}
                for previous in (row(value) for value in old_order):
                    if previous["line_id"] not in present and previous["reserved"]:
                        self.freeze(c, self.pool(c, t, previous["pool_id"]), "ORDER_LINE_REMOVAL_REVIEW")
                for line in order["lines"]:
                    mapping = by_variant.get(line["variant_id"])
                    if not mapping:
                        if line["sku"] and any(self.pool(c, t, v["pool_id"])["sku"] == line["sku"] for v in mappings):
                            for v in mappings:
                                self.freeze(c, self.pool(c, t, v["pool_id"]), "SKU_MAPPING_REQUIRED")
                        continue
                    pool = self.pool(c, t, mapping["pool_id"])
                    if pool["pool_type"] != "merchant" or not pool["initialized"]:
                        continue
                    if line["shipped"] is None or line["cancelled"] is None or line["shipped"] + line["cancelled"] > line["ordered"]:
                        self.freeze(c, pool, "ORDER_QUANTITY_UNKNOWN")
                        continue
                    previous = row(c.execute(select(inventory_positions).where(inventory_positions.c.tenant_id == t,
                        inventory_positions.c.connection_id == connection_id, inventory_positions.c.order_id == order["id"],
                        inventory_positions.c.line_id == line["id"]).with_for_update()).first())
                    fingerprint = digest(canonical({"line": line, "cancelled": order["cancelled"]}))
                    observed_time = order["updated_at"] or order["created_at"] or ""
                    if previous and (previous["fingerprint"] == fingerprint or source_time(observed_time) < source_time(previous["source_time"])):
                        continue
                    shipped_delta = line["shipped"] - (previous["shipped"] if previous else 0)
                    if not previous and source_time(order["created_at"]) < pool["created_at"]:
                        shipped_delta = 0  # Physical opening balance already includes historical shipments.
                    if shipped_delta < 0:
                        self.freeze(c, pool, "PHYSICAL_RETURN_REQUIRES_APPROVAL")
                        continue
                    if shipped_delta > pool["on_hand"]:
                        self.freeze(c, pool, "PHYSICAL_STOCK_SHORTAGE")
                        continue
                    reserved = max(0, line["ordered"] - line["shipped"] - line["cancelled"])
                    values = dict(ordered=line["ordered"], shipped=line["shipped"], reserved=reserved,
                        source_time=observed_time, fingerprint=fingerprint, updated_at=clock())
                    if previous:
                        c.execute(update(inventory_positions).where(inventory_positions.c.id == previous["id"]).values(**values))
                        position_id = previous["id"]
                    else:
                        position_id = uid("position")
                        c.execute(inventory_positions.insert().values(id=position_id, tenant_id=t, pool_id=pool["id"],
                            connection_id=connection_id, order_id=order["id"], line_id=line["id"], created_at=clock(), **values))
                    on_hand = pool["on_hand"] - shipped_delta
                    c.execute(update(inventory_pools).where(inventory_pools.c.id == pool["id"]).values(on_hand=on_hand, version=pool["version"] + 1))
                    reserved_total = self.reserved(c, t, pool["id"])
                    c.execute(stock_journal.insert().values(id=uid("stock"), tenant_id=t, pool_id=pool["id"], kind="order_observed",
                        delta=-shipped_delta, on_hand_after=on_hand, reserved_after=reserved_total, reference=position_id,
                        idem="position-" + position_id + "-" + digest(fingerprint + observed_time)[:24], created_at=clock()))
                    self.db.audit(c, t, "inventory", "stock.order.accounted", position_id,
                        {"pool_id": pool["id"], "shipped_delta": shipped_delta, "reserved": reserved, "order_id": order["id"], "connection_id": connection_id})
                    if reserved_total + pool["safety"] > on_hand:
                        self.freeze(c, pool, "RESERVATIONS_EXCEED_STOCK")
            for identifier in {m["pool_id"] for m in mappings}:
                pool = self.pool(c, t, identifier)
                if pool["pool_type"] == "merchant" and pool["initialized"]:
                    offers = c.execute(select(func.coalesce(func.sum(sku_mappings.c.quota), 0)).where(
                        sku_mappings.c.tenant_id == t, sku_mappings.c.pool_id == identifier)).scalar()
                    if offers > max(0, pool["on_hand"]-self.reserved(c, t, identifier)-pool["safety"]):
                        self.freeze(c, pool, "CHANNEL_QUOTAS_EXCEED_STOCK")
        return issues

    def prepare_adjustment(self, t, p):
        with self.db.engine.connect() as c:
            pool = self.pool(c, t, p.get("pool_id"))
            if pool["pool_type"] != "merchant":
                self.error("平台仓实物库存由平台维护")
            if type(p.get("quantity")) is not int or not 0 <= p["quantity"] <= 1000000:
                self.error("盘点后的实物库存须为 0–1000000 的整数")
            safety = p.get("safety", pool["safety"])
            if type(safety) is not int or not 0 <= safety <= 1000000:
                self.error("安全库存无效")
            return {"on_hand": pool["on_hand"], "safety": pool["safety"], "version": pool["version"]}, dict(p, safety=safety)

    def execute_adjustment(self, action):
        p, t = action["payload"], action["tenant_id"]
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            pool = self.pool(c, t, p["pool_id"])
            if {k: pool[k] for k in ("on_hand", "safety", "version")} != action["before"]:
                self.error("库存池已变化，请重新盘点并审批")
            reserved = self.reserved(c, t, pool["id"])
            c.execute(stock_journal.insert().values(id=uid("stock"), tenant_id=t, pool_id=pool["id"], kind="approved_adjustment",
                delta=p["quantity"]-pool["on_hand"], on_hand_after=p["quantity"], reserved_after=reserved, reference=action["id"], idem=action["id"], created_at=clock()))
            c.execute(update(inventory_pools).where(inventory_pools.c.id == pool["id"]).values(on_hand=p["quantity"], safety=p["safety"], initialized=True, version=pool["version"]+1))
            if reserved + p["safety"] > p["quantity"]:
                self.freeze(c, pool, "RESERVATIONS_EXCEED_STOCK")
            self.db.audit(c, t, action["approved_by"], "stock.adjusted", action["id"], {"pool_id": pool["id"], "on_hand": p["quantity"], "safety": p["safety"]})
        for mapping in self.db.list(sku_mappings, t, 1000, pool_id=p["pool_id"]):
            self.ingest(mapping["connection_id"], t)
        return {"pool_id": pool["id"], "on_hand": p["quantity"], "safety": p["safety"]}

    def create_plan(self, actor, pool_id, targets):
        t = actor["tenant_id"]
        initial = self.db.get(inventory_pools, pool_id, t)
        if not initial:
            self.error("共享库存池不存在")
        mappings = self.db.list(sku_mappings, t, 100, pool_id=pool_id)
        if not mappings or set(targets) != {mapping["id"] for mapping in mappings}:
            self.error("分配方案须明确填写库存池的全部店铺映射")
        if len(mappings) > 20 or any(type(value) is not int or not 0 <= value <= 1000000 for value in targets.values()):
            self.error("分配数量无效，单个方案最多 20 个店铺规格")
        observed = {}
        for mapping in mappings:
            connection = self.db.get(connections, mapping["connection_id"], t)
            if not connection or connection["status"] != "active":
                self.error("某个店铺尚未开放库存分配能力")
            api = self.ops.client(mapping["connection_id"], t)
            if "inventory" not in api.capabilities()["operations"]:
                self.error("某个店铺尚未开放库存分配能力")
            if not self.source_ready(connection):
                self.error("关联店铺需在最近 5 分钟内完整同步订单与库存")
            value = api.inventory_position(mapping["item_id"], mapping["location_id"])
            if value["pool_type"] != "merchant" or type(value["quantity"]) is not int:
                self.error("该库存地点不支持商家仓配额调整")
            observed[mapping["id"]] = value["quantity"]
        plan_id = uid("allocation")
        steps = [{"mapping_id": mapping["id"], "connection_id": mapping["connection_id"], "before": observed[mapping["id"]],
            "quantity": targets[mapping["id"]], "task_id": uid("task"), "phase": "decrease" if targets[mapping["id"]] < observed[mapping["id"]] else "increase",
            "item_id": mapping["item_id"], "location_id": mapping["location_id"]} for mapping in mappings if targets[mapping["id"]] != observed[mapping["id"]]]
        steps.sort(key=lambda value: (value["phase"] != "decrease", value["mapping_id"]))
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            pool = self.pool(c, t, pool_id)
            if pool["version"] != initial["version"]:
                self.error("库存池在回查期间已变化，请重试生成方案")
            if pool["pool_type"] != "merchant" or not pool["initialized"]:
                self.error("请先完成商家仓的实物库存盘点与独立审批")
            if sum(targets.values()) > max(0, pool["on_hand"] - self.reserved(c, t, pool_id) - pool["safety"]):
                self.error("所有店铺配额合计超过库存池可分配数量")
            if pool["frozen"] and any(step["phase"] == "increase" for step in steps):
                self.error("库存池存在待核实问题，已冻结增库存")
            if c.execute(select(stock_plans.c.id).where(stock_plans.c.tenant_id == t, stock_plans.c.pool_id == pool_id,
                stock_plans.c.status.in_(["pending", "running", "needs_review"]))).first():
                self.error("该库存池已有未结案分配方案")
            plan = dict(id=plan_id, tenant_id=t, pool_id=pool_id, actor_id=actor["id"], version=pool["version"], steps=steps,
                status="pending" if steps else "completed", expires_at=clock()+86400, created_at=clock())
            c.execute(stock_plans.insert().values(**plan))
            decreases = [step["task_id"] for step in steps if step["phase"] == "decrease"]
            for step in steps:
                task = dict(id=step["task_id"], tenant_id=t, actor_id=actor["id"], connection_id=step["connection_id"],
                    command=f"共享库存 {pool['sku']}：{step['before']} → {step['quantity']}", kind="inventory",
                    params={"item_id": step["item_id"], "location_id": step["location_id"], "quantity": step["quantity"],
                        "_stock_plan": plan_id, "depends_on": decreases if step["phase"] == "increase" else []},
                    status="queued", result={}, idem=plan_id+"-"+step["mapping_id"], created_at=clock())
                c.execute(tasks.insert().values(**task))
                self.db.enqueue(c, t, "task", task["id"])
                self.db.audit(c, t, actor["id"], "task.created", task["id"], {"kind": "inventory", "stock_plan": plan_id})
            self.db.audit(c, t, actor["id"], "stock.plan.created", plan_id, {"pool_id": pool_id, "version": pool["version"], "targets": targets, "decrease_before_increase": True})
        return plan

    def guard_task(self, task):
        if task["kind"] != "inventory":
            return
        t, p = task["tenant_id"], task["params"]
        mappings = self.db.list(sku_mappings, t, 1, connection_id=task["connection_id"], item_id=p.get("item_id"), location_id=p.get("location_id"))
        if not mappings:
            if p.get("_stock_plan"):
                self.error("分配方案的仓库映射已失效")
            return
        plan = self.db.get(stock_plans, p.get("_stock_plan"), t)
        if not plan or not any(step["task_id"] == task["id"] and step["connection_id"] == task["connection_id"]
            and step["quantity"] == p.get("quantity") and step["item_id"] == p.get("item_id") and step["location_id"] == p.get("location_id") for step in plan["steps"]):
            self.error("此规格属于共享库存池，请通过分配方案调整")
        pool = self.db.get(inventory_pools, plan["pool_id"], t)
        if plan["status"] == "closed" or not pool or pool["version"] != plan["version"] or plan["expires_at"] <= clock():
            self.error("库存池版本变化或分配方案过期，请重新生成")
        step = next(step for step in plan["steps"] if step["task_id"] == task["id"])
        if step["phase"] == "increase":
            if pool["frozen"]:
                self.error("库存池已冻结增库存")
            for earlier in (value for value in plan["steps"] if value["phase"] == "decrease"):
                dependency = self.db.get(tasks, earlier["task_id"], t)
                if not dependency or dependency["status"] != "completed":
                    self.error("必须先完成并回读全部减库存操作")

    def action_outcome(self, action):
        task = self.db.get(tasks, action["task_id"], action["tenant_id"])
        identifier = task["params"].get("_stock_plan") if task else None
        if not identifier:
            return
        t = action["tenant_id"]
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            plan = self.db.get(stock_plans, identifier, t, c)
            if not plan or plan["status"] == "closed":
                return
            pool = self.pool(c, t, plan["pool_id"])
            current = [self.db.get(tasks, step["task_id"], t, c) for step in plan["steps"]]
            if all(value and value["status"] == "completed" for value in current):
                status = "completed"
            elif any(value and value["status"] in ("failed", "needs_review", "expired", "rejected") for value in current):
                status = "needs_review"
                self.freeze(c, pool, "ALLOCATION_REQUIRES_REVIEW")
            else:
                status = "running"
            if action["status"] in ("executed", "reconciled"):
                step = next(step for step in plan["steps"] if step["task_id"] == task["id"])
                c.execute(update(sku_mappings).where(sku_mappings.c.id == step["mapping_id"], sku_mappings.c.tenant_id == t).values(quota=step["quantity"]))
            c.execute(update(stock_plans).where(stock_plans.c.id == identifier).values(status=status))

    def close_plan(self, actor, plan_id, note):
        t = actor["tenant_id"]
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            plan = self.db.get(stock_plans, plan_id, t, c)
            if not plan:
                self.error("分配方案不存在")
            for step in plan["steps"]:
                related = self.db.list(actions, t, 5, task_id=step["task_id"])
                if any(value["status"] in ("approved", "executing", "uncertain") for value in related):
                    self.error("先核实或处理所有已批准、执行中与结果未知的操作")
                c.execute(update(tasks).where(tasks.c.id == step["task_id"], tasks.c.tenant_id == t,
                    tasks.c.status.in_(["queued", "awaiting_approval"])).values(status="rejected"))
                c.execute(update(actions).where(actions.c.task_id == step["task_id"], actions.c.tenant_id == t,
                    actions.c.status == "pending").values(status="rejected", note=note, approved_by=actor["id"]))
            c.execute(update(stock_plans).where(stock_plans.c.id == plan_id, stock_plans.c.tenant_id == t).values(status="closed"))
            self.db.audit(c, t, actor["id"], "stock.plan.closed", plan_id, {"note": note})
        return {"status": "closed"}

    def clear_freeze(self, actor, pool_id, note):
        t = actor["tenant_id"]
        initial = self.db.get(inventory_pools, pool_id, t)
        if not initial or not initial["initialized"]:
            self.error("请先完成库存池初始化")
        mappings = self.db.list(sku_mappings, t, 1000, pool_id=pool_id)
        offers = 0
        for mapping in mappings:
            connection = self.db.get(connections, mapping["connection_id"], t)
            if not self.source_ready(connection, initial["pool_type"]):
                self.error("请先完整同步所有关联店铺")
            if initial["pool_type"] == "merchant":
                for record in self.db.list(resources, t, 10000, connection_id=mapping["connection_id"], kind="order"):
                    order = normalize_resource(connection["provider"], "order", record["data"])
                    if order["environment"] != "sandbox_static" and order["fulfillment_channel"] == "unknown":
                        self.error("订单履约渠道尚未确认，不能解除冻结")
                    if order["fulfillment_channel"] != "merchant" or order["environment"] == "sandbox_static":
                        continue
                    if not source_time(order["created_at"]) or not source_time(order["updated_at"]):
                        self.error("订单时间尚未确认，不能解除冻结")
                    if not order["lines_complete"] and not (order["cancelled"] or order["fulfillment_status"] == "FULFILLED"):
                        self.error("关联店铺仍有不完整订单明细，不能解除冻结")
                    for line in order["lines"]:
                        if line["variant_id"] == mapping["variant_id"]:
                            positions = self.db.list(inventory_positions, t, 1, connection_id=mapping["connection_id"], order_id=order["id"], line_id=line["id"])
                            fingerprint = digest(canonical({"line": line, "cancelled": order["cancelled"]}))
                            if not positions or positions[0]["fingerprint"] != fingerprint:
                                self.error("订单台账与最新明细未吻合，请核实数量及映射后重新同步")
            value = self.ops.client(mapping["connection_id"], t).inventory_position(mapping["item_id"], mapping["location_id"])
            offers += max(0, value["quantity"])
        with self.db.transaction() as c:
            self.lock_tenant(c, t)
            pool = self.pool(c, t, pool_id)
            if pool["version"] != initial["version"]:
                self.error("回查期间库存池已变化，请重新核实")
            if c.execute(select(stock_plans.c.id).where(stock_plans.c.tenant_id == t, stock_plans.c.pool_id == pool_id,
                stock_plans.c.status.in_(["pending", "running", "needs_review"]))).first():
                self.error("先核实并结案旧分配方案")
            if pool["pool_type"] == "merchant" and (self.reserved(c, t, pool_id)+pool["safety"] > pool["on_hand"] or offers > max(0, pool["on_hand"] - self.reserved(c, t, pool_id) - pool["safety"])):
                self.error("平台可售数量超过可分配库存，请先创建只减库存的修正方案")
            c.execute(update(inventory_pools).where(inventory_pools.c.id == pool_id).values(frozen=False, issues=[]))
            self.db.audit(c, t, actor["id"], "stock.freeze.cleared", pool_id, {"note": note, "observed_offers": offers})
        return {"status": "active"}
