import json
import re
from .security import digest
from decimal import Decimal, InvalidOperation
from sqlalchemy import select, update, and_, or_
from .database import (tasks, actions, jobs, connections, resources, tenants, users, knowledge,
                       clock, uid, row, canonical)
from .shopify import ShopifyError, upsert_resource
from .platforms.contracts import PlatformError, normalize_resource, PROVIDERS
from .platforms.registry import connection_adapter, provider_spec

class BusinessError(ValueError): pass
def cents(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0 or n != n.quantize(Decimal(".01")): raise ValueError()
        return int(n*100)
    except (InvalidOperation, ValueError, TypeError): raise BusinessError("金额必须为正数，最多两位小数") from None

def order_snapshot(order):
    return {k:order.get(k) for k in ("id","test","updatedAt","lineItems","cancelledAt","displayFinancialStatus","displayFulfillmentStatus","totalRefundedSet","netPaymentSet")}

class Operations:
    def __init__(self, db, vault, settings, client_factory=None):
        self.db, self.vault, self.settings = db, vault, settings
        self.client_factory = client_factory
        from .inventory import SharedInventory
        self.shared = SharedInventory(db, self)

    def client(self, conn, tenant):
        return connection_adapter(self.db, self.vault, conn, tenant, self.client_factory)

    def submit(self, actor, command, kind, params, connection_id=None, idem=None):
        allowed = {"sync","analyze","chat","learn","inventory","fulfill","tracking","cancel","refund","product","pool_adjust","mcf_create","mcf_cancel","sandbox_sku","sandbox_stock","sandbox_status","sandbox_package","command"}
        if kind not in allowed: raise BusinessError("该平台能力未开放")
        if kind not in ("chat", "analyze", "learn", "command", "pool_adjust"):
            connection = self.db.get(connections, connection_id, actor["tenant_id"])
            if not connection or connection["status"] != "active" or connection["provider"] not in PROVIDERS:
                raise BusinessError("请选择本组织已接通的电商店铺")
        if kind == "pool_adjust":
            from .database import inventory_pools
            if not self.db.get(inventory_pools, params.get("pool_id"), actor["tenant_id"]):
                raise BusinessError("共享库存池不存在")
            connection_id = params["pool_id"]
        key = idem or uid("request")
        with self.db.transaction() as db:
            db.execute(select(tenants).where(tenants.c.id==actor["tenant_id"]).with_for_update()).first()
            old = row(db.execute(select(tasks).where(tasks.c.tenant_id == actor["tenant_id"],tasks.c.idem == key)).first())
            if old:
                if old["kind"] != kind or old["params"] != params or old["connection_id"] != connection_id or old["command"] != command:
                    raise BusinessError("幂等键已用于不同请求")
                return old
            task = dict(id=uid("task"),tenant_id=actor["tenant_id"],actor_id=actor["id"],connection_id=connection_id,
                command=command,kind=kind,params=params,status="queued",result={},idem=key,created_at=clock())
            db.execute(tasks.insert().values(**task))
            self.db.enqueue(db,actor["tenant_id"],"task",task["id"])
            self.db.audit(db,actor["tenant_id"],actor["id"],"task.created",task["id"],{"kind":kind,"connection_id":connection_id})
        return task

    def sync(self, task):
        t, c = task["tenant_id"], task["connection_id"]
        api = self.client(c, t)
        connection = self.db.get(connections, c, t)
        provider = connection["provider"]
        started = clock()
        errors, orders_data, variants = [], None, None
        if provider == "shopify":
            capability = api.capability()
            shop = capability["shop"]
            scopes = [s["handle"] for s in capability["currentAppInstallation"]["accessScopes"]]
            order_loader = lambda: api.orders(task["params"].get("search", ""))
            product_loader = api.variants
        else:
            capability = api.capabilities()
            shop = {"name": connection["info"].get("label", provider_spec(provider).name)}
            scopes = []
            order_loader = lambda: api.snapshot_orders(task["params"])
            product_loader = api.snapshot_products
        try:
            orders_data = order_loader()
        except PlatformError as error:
            errors.append({"component": "orders", "code": error.code})
        try:
            variants = product_loader()
        except PlatformError as error:
            errors.append({"component": "variants", "code": error.code})
        if orders_data is None and variants is None:
            raise PlatformError("SYNC_ALL_COMPONENTS_FAILED")
        with self.db.transaction() as db:
            db.execute(select(tenants.c.id).where(tenants.c.id == t).with_for_update()).first()
            conn = row(db.execute(select(connections).where(connections.c.id == c, connections.c.tenant_id == t).with_for_update()).first())
            if conn["info"].get("sync_started_at", 0) > started:
                return {"summary": "已有更新的同步快照，较旧请求已忽略。", "source": provider}
            blocked = {r[0] for r in db.execute(select(resources.c.external_id).where(resources.c.tenant_id == t,
                resources.c.connection_id == c, resources.c.kind == "blocked_order"))}
            for value in orders_data or []:
                if digest(value["id"]) not in blocked:
                    upsert_resource(db, t, c, "order", value)
            if variants is not None:
                db.execute(resources.delete().where(resources.c.tenant_id == t, resources.c.connection_id == c, resources.c.kind == "variant"))
                for value in variants:
                    upsert_resource(db, t, c, "variant", value)
            info = dict(conn["info"], shop=shop, scopes=scopes, last_sync=clock(), sync_started_at=started,
                sync_errors=errors, order_access=orders_data is not None, capabilities=api.capabilities())
            db.execute(update(connections).where(connections.c.id == c, connections.c.tenant_id == t).values(info=info))
            self.db.audit(db, t, "worker", provider + ".synced", c, {"orders": len(orders_data or []), "variants": len(variants or []), "errors": errors})
        stock_issues = self.shared.ingest(c, t)
        note = "；部分同步失败：" + "、".join(e["component"] + " / " + e["code"] for e in errors) if errors else ""
        return {"summary": f"已同步 {len(orders_data or [])} 笔订单与 {len(variants or [])} 个商品规格{note}。范围受平台授权与沙箱类型限制。",
            "orders": len(orders_data or []), "variants": len(variants or []), "partial": bool(errors), "errors": errors,
            "stock_issues": stock_issues, "source": provider_spec(provider).name + " API", "capabilities": api.capabilities()}

    def analyze(self, task):
        from .analysis import AnalysisRunner
        return AnalysisRunner(self.db).run(task)

    def learn(self,task):
        t=task["tenant_id"]
        tickets=self.db.list(resources,t,20000,kind="ticket")
        categories=[("能源效率",("energy","efficien","能效","能源","energie")),("本地退货",("return","退货","retour","local"))]
        groups={}
        for record in tickets:
            data=record["data"];text=data["text"].lower()
            for topic,words in categories:
                if any(w in text for w in words):groups.setdefault((data["market"],topic),[]).append(record)
        drafts=[]
        with self.db.transaction() as db:
            for (market,topic),samples in groups.items():
                if len(samples)<5:continue
                title=market+" 客户关注："+topic
                old=row(db.execute(select(knowledge).where(knowledge.c.tenant_id==t,knowledge.c.title==title,knowledge.c.market==market)).first())
                if old:continue
                content=f"在导入的客服记录中发现 {len(samples)} 条与{topic}相关的咨询。\n\n可复用步骤：\n1. 检索该 SKU 已确认的产品规格或退货政策。\n2. 引用政策版本与适用地区回答。\n3. 未确认时交由人工处理，不承诺能效等级或本地退货资格。\n\n这是待确认的经验草稿；启用前请核对原始证据。"
                kid=uid("skill");evidence=[r["external_id"]+"："+r["data"]["text"][:250] for r in samples[:30]]
                db.execute(knowledge.insert().values(id=kid,tenant_id=t,title=title,market=market,content=content,evidence=evidence,active=False,created_at=clock()))
                drafts.append(kid)
                self.db.audit(db,t,"worker","knowledge.learned",kid,{"market":market,"category":topic,"evidence_count":len(samples),"active":False})
        return {"summary":f"分析 {len(tickets)} 条实际导入的客服记录，生成 {len(drafts)} 条技能草稿。请核对证据后启用；重复学习不会重复生成。","draft_ids":drafts,"source":"组织导入的客服记录"}

    def propose(self, task):
        t, c, p, kind = task["tenant_id"], task["connection_id"], task["params"], task["kind"]
        existing = self.db.list(actions, t, 1, task_id=task["id"])
        if existing:
            return {"summary": "已生成操作方案，等待审批。", "action_id": existing[0]["id"]}
        if kind == "pool_adjust":
            before, p = self.shared.prepare_adjustment(t, p)
            platform = "共享仓库"
        else:
            self.shared.guard_task(task)
            api = self.client(c, t)
            if kind not in api.capabilities()["operations"]:
                raise BusinessError("该平台能力未开放")
            if p.get("order_id") and self.db.list(resources, t, 1, connection_id=c, kind="blocked_order", external_id=digest(p["order_id"])):
                raise BusinessError("该订单已按隐私请求停用，请在平台人工处理")
            before, p = api.prepare_action(kind, p, self.settings.test_only)
            platform = provider_spec(api.provider).name
            if p.get("_stock_plan"):
                from .database import stock_plans
                plan = self.db.get(stock_plans, p["_stock_plan"], t)
                step = next(s for s in plan["steps"] if s["task_id"] == task["id"])
                if before.get("quantity") != step["before"]:
                    raise BusinessError("平台库存与分配方案不一致，请重新生成")
        titles = {"fulfill": "订单履约", "tracking": "更新物流单号", "cancel": "取消订单（不退款）", "refund": "订单退款",
            "inventory": "调整库存", "product": "更新产品说明", "pool_adjust": "盘点 / 收货 / 退货入库",
            "sandbox_sku": "创建 Amazon 沙箱测试 SKU", "sandbox_stock": "增加 Amazon 沙箱虚拟库存", "mcf_create": "创建 Amazon 沙箱 MCF 履约",
            "mcf_cancel": "取消 Amazon 沙箱 MCF 履约", "sandbox_status": "推进 Amazon 沙箱履约状态", "sandbox_package": "推进 Amazon 沙箱包裹状态"}
        action = dict(id=uid("action"), tenant_id=t, task_id=task["id"], connection_id=c, actor_id=task["actor_id"], kind=kind,
            title=platform + " · " + titles[kind], payload=p, before=before, status="pending",
            reason="业务写入需要独立审批人；执行前再次校验店铺、权限与业务状态。", approved_by=None, note="", result={},
            expires_at=clock()+86400, created_at=clock())
        with self.db.transaction() as db:
            db.execute(actions.insert().values(**action))
            self.db.audit(db, t, task["actor_id"], "action.proposed", action["id"], {"kind": kind, "connection_id": c, "before": before, "after": p})
            db.execute(update(tasks).where(tasks.c.id == task["id"], tasks.c.tenant_id == t).values(status="awaiting_approval"))
            self.db.enqueue(db, t, "notify", action["id"])
        return {"summary": "已生成操作方案，等待独立审批。", "action_id": action["id"]}

    def check_order(self, order):
        check_test_order(order, self.settings.test_only)

    def refund_parent(self, order, amount):
        return refund_transaction(order, amount, self.settings.test_only)

    def decide(self, actor, action_id, approved, note=""):
        t=actor["tenant_id"]
        with self.db.transaction() as db:
            a=row(db.execute(select(actions).where(actions.c.id==action_id,actions.c.tenant_id==t).with_for_update()).first())
            if not a: raise BusinessError("操作不存在")
            if a["status"] != "pending": raise BusinessError("操作已处理")
            if a["expires_at"]<=clock(): raise BusinessError("审批已过期，请生成新方案")
            if approved and a["actor_id"]==actor["id"]: raise BusinessError("申请人与审批人必须是不同成员")
            status="approved" if approved else "rejected"
            db.execute(update(actions).where(actions.c.id==action_id).values(status=status,approved_by=actor["id"],note=note))
            self.db.audit(db,t,actor["id"],"approval.decided",action_id,{"approved":approved,"note":note})
            if approved: self.db.enqueue(db,t,"action",action_id)
            else:
                db.execute(update(tasks).where(tasks.c.id==a["task_id"],tasks.c.tenant_id==t).values(status="rejected"))
                self.db.enqueue(db,t,"notify_result",action_id)
        self.shared.action_outcome(self.db.get(actions, action_id, t))
        self.refresh_commands(t)
        return {"ok":True,"status":status}

    def execute(self, action_id, tenant_id):
        with self.db.transaction() as db:
            db.execute(select(tenants.c.id).where(tenants.c.id == tenant_id).with_for_update()).first()
            a = row(db.execute(select(actions).where(actions.c.id == action_id, actions.c.tenant_id == tenant_id).with_for_update()).first())
            if not a or a["status"] in ("executed", "reconciled", "rejected", "failed", "expired"):
                return
            if a["status"] in ("executing", "uncertain"):
                raise BusinessError("外部结果待核实，禁止重复发送")
            if a["status"] != "approved" or a["expires_at"] <= clock():
                raise BusinessError("操作尚未批准或审批已过期")
            for identifier, roles in ((a["approved_by"], ("owner", "approver")), (a["actor_id"], ("owner", "approver", "operator"))):
                member = self.db.get(users, identifier, tenant_id, db)
                if not member or not member["active"] or member["role"] not in roles:
                    raise BusinessError("申请人或审批人权限已失效")
            db.execute(update(actions).where(actions.c.id == action_id).values(status="executing"))
            self.db.audit(db, tenant_id, "worker", "action.started", action_id, {"kind": a["kind"], "connection_id": a["connection_id"]})
        try:
            if a["kind"] == "pool_adjust":
                result = self.shared.execute_adjustment(a)
            else:
                task = self.db.get(tasks, a["task_id"], tenant_id)
                self.shared.guard_task(task)
                api = self.client(a["connection_id"], tenant_id)
                if a["kind"] not in api.capabilities()["operations"]:
                    raise BusinessError("平台能力或授权已失效")
                result = api.execute_action(a["kind"], a["payload"], a["before"], action_id, self.settings.test_only)
            status = "uncertain" if result.get("pending_verification") else "executed"
            with self.db.transaction() as db:
                db.execute(update(actions).where(actions.c.id == action_id, actions.c.tenant_id == tenant_id).values(status=status, result=result))
                db.execute(update(tasks).where(tasks.c.id == a["task_id"], tasks.c.tenant_id == tenant_id).values(
                    status="completed" if status == "executed" else "needs_review", result={"summary": "操作执行成功" if status == "executed" else "平台操作已提交，须核实结果", "action_id": action_id}))
                self.db.audit(db, tenant_id, "worker", "action."+status, action_id, {"kind": a["kind"], "result": result})
                self.db.enqueue(db, tenant_id, "notify_result", action_id)
                if a["kind"] in ("inventory", "fulfill", "tracking", "cancel", "refund", "product", "sandbox_sku", "sandbox_stock", "mcf_create", "mcf_cancel", "sandbox_status", "sandbox_package"):
                    # Read-only refresh after a write, never a repeat of the write itself.
                    self.db.enqueue(db, tenant_id, "refresh_connection", action_id)
            self.shared.action_outcome(self.db.get(actions, action_id, tenant_id))
            self.refresh_commands(tenant_id)
        except (BusinessError, PlatformError) as error:
            self.action_failure(a, str(error), isinstance(error, PlatformError) and error.unknown)
            raise
        except Exception:
            self.action_failure(a, "执行结果未知，请核查平台记录", True)
            raise

    def action_failure(self,a,message,uncertain=False):
        status="uncertain" if uncertain else "failed"
        with self.db.transaction() as db:
            db.execute(update(actions).where(actions.c.id==a["id"],actions.c.tenant_id==a["tenant_id"]).values(status=status,reason=message))
            db.execute(update(tasks).where(tasks.c.id==a["task_id"],tasks.c.tenant_id==a["tenant_id"]).values(status="needs_review" if uncertain else "failed",result={"summary":message}))
            self.db.audit(db,a["tenant_id"],"worker","action."+status,a["id"],{"reason":message})
        self.shared.action_outcome(self.db.get(actions, a["id"], a["tenant_id"]))
        self.refresh_commands(a["tenant_id"])

    def reconcile(self, actor, action_id, note):
        t = actor["tenant_id"]
        a = self.db.get(actions, action_id, t)
        if not a or a["status"] != "uncertain":
            raise BusinessError("仅结果待核实的操作可结案")
        expected = a["before"].get("quantity", 0) + a["payload"].get("quantity", 0) if a["kind"] == "sandbox_stock" else None
        p = dict(a["payload"], _action_id=action_id, _expected_quantity=a["result"].get("expected_quantity", expected))
        if a["kind"] == "pool_adjust":
            from .database import stock_journal
            proof = self.db.list(stock_journal, t, 1, idem=action_id)
            matched = bool(proof)
            proof = {"journal": proof[0]["id"]} if proof else {}
        else:
            matched, proof = self.client(a["connection_id"], t).verify_action(a["kind"], p)
        if not matched:
            raise BusinessError("平台结果尚不吻合，禁止结案或再次自动发送；请在平台人工核实")
        with self.db.transaction() as db:
            current = row(db.execute(select(actions).where(actions.c.id == action_id, actions.c.tenant_id == t).with_for_update()).first())
            if current["status"] != "uncertain":
                raise BusinessError("操作状态已变化")
            db.execute(update(actions).where(actions.c.id == action_id).values(status="reconciled", note=note, result={"observed": proof, "verified_by": actor["id"]}))
            db.execute(update(tasks).where(tasks.c.id == a["task_id"], tasks.c.tenant_id == t).values(status="completed",
                result={"summary": "经人工触发只读核实，平台状态与目标吻合；没有再次发送写入。", "action_id": action_id}))
            self.db.audit(db, t, actor["id"], "action.reconciled", action_id, {"observed": proof, "note": note, "attribution": "provider_evidence" if a["kind"] in ("refund", "mcf_create", "pool_adjust") else "state_match_only"})
            self.db.enqueue(db, t, "notify_result", action_id)
        self.shared.action_outcome(self.db.get(actions, action_id, t))
        self.refresh_commands(t)
        return {"ok": True, "status": "reconciled"}

    def process_task(self, task_id,tenant_id):
        task=self.db.get(tasks,task_id,tenant_id)
        if not task or task["status"] not in ("queued","running"): return
        with self.db.transaction() as db:
            db.execute(update(tasks).where(tasks.c.id==task_id,tasks.c.tenant_id==tenant_id).values(status="running"))
        try:
            if task["kind"]=="sync": result=self.sync(task)
            elif task["kind"] in ("analyze","chat"): result=self.analyze(task)
            elif task["kind"]=="learn": result=self.learn(task)
            elif task["kind"]=="command": result=self.process_command(task)
            else: result=self.propose(task)
            with self.db.transaction() as db:
                current=self.db.get(tasks,task_id,tenant_id,db)
                status = "needs_input" if result.get("needs_clarification") else "running" if result.get("dispatched") else "awaiting_approval" if current["status"]=="awaiting_approval" else "partial" if result.get("partial") else "completed"
                db.execute(update(tasks).where(tasks.c.id==task_id,tasks.c.tenant_id==tenant_id).values(status=status,result=result))
                self.db.audit(db,tenant_id,"worker","task.finished",task_id,{"kind":task["kind"]})
                if task["params"].get("notify_open_id") and (task["kind"] != "command" or status == "needs_input"):self.db.enqueue(db,tenant_id,"notify_task",task_id)
            self.refresh_commands(tenant_id)
        except (BusinessError,PlatformError) as e:
            self.fail_task(task_id,tenant_id,str(e));raise
        except Exception:
            self.fail_task(task_id,tenant_id,"任务失败，可重试；未授予额外业务权限");raise

    def refresh_commands(self, tenant_id):
        from .commands import refresh_commands
        refresh_commands(self, tenant_id)

    def process_command(self, task):
        from .commands import dispatch
        return dispatch(self, task)

    def fail_task(self,task_id,tenant_id,message):
        with self.db.transaction() as db:
            current=self.db.get(tasks,task_id,tenant_id,db)
            result=dict(current["result"],summary=message) if current and current["kind"] in ("chat","analyze") else {"summary":message}
            db.execute(update(tasks).where(tasks.c.id==task_id,tasks.c.tenant_id==tenant_id).values(status="failed",result=result))
            self.db.audit(db,tenant_id,"worker","task.failed",task_id,{"reason":message})
            task=self.db.get(tasks,task_id,tenant_id,db)
            if task and task["params"].get("notify_open_id"):self.db.enqueue(db,tenant_id,"notify_task",task_id)
        if task:
            self.shared.action_outcome({"tenant_id": tenant_id, "task_id": task_id, "status": "failed"})
        self.refresh_commands(tenant_id)

def valid_gid(value, kind):
    if not isinstance(value,str) or not re.fullmatch(r"gid://shopify/"+kind+r"/\d+",value): raise BusinessError("业务对象 ID 无效")
    return value

def cents_nonnegative(v):
    try:
        x=Decimal(str(v))
        if not x.is_finite() or x<0 or x!=x.quantize(Decimal(".01")): raise ValueError()
        return int(x*100)
    except (InvalidOperation,ValueError,TypeError): raise BusinessError("平台金额格式无效") from None

def check_test_order(order, test_only):
    if test_only and not order.get("test"):
        raise BusinessError("当前为测试保护模式，真实订单写入已禁止")

def refund_transaction(order, amount, test_only):
    check_test_order(order, test_only)
    net = cents_nonnegative(order["netPaymentSet"]["shopMoney"]["amount"])
    if amount > net:
        raise BusinessError("退款金额超过订单剩余可退金额")
    valid = [x for x in order["transactions"] if x["kind"] in ("SALE", "CAPTURE") and x["status"] == "SUCCESS"]
    if test_only and (not valid or any(not x.get("test") for x in valid)):
        raise BusinessError("订单包含非测试收款交易，退款已禁止")
    for parent in valid:
        paid = cents_nonnegative(parent["amountSet"]["shopMoney"]["amount"])
        refunded = sum(cents_nonnegative(x["amountSet"]["shopMoney"]["amount"]) for x in order["transactions"]
            if x["kind"] == "REFUND" and x["status"] in ("SUCCESS", "PENDING") and (x.get("parentTransaction") or {}).get("id") == parent["id"])
        if paid - refunded >= amount:
            return parent
    raise BusinessError("找不到足够余额的成功收款交易，请人工拆分退款")

def redact(v):
    references = {}
    def preserve(match):
        token = "AMAZONORDERREF" + chr(65 + len(references))
        references[token] = match.group(0)
        return token
    v = re.sub(r"\b[0-9]{3}-[0-9]{7}-[0-9]{7}\b", preserve, v)
    v=re.sub(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}","[邮箱已隐去]",v)
    v = re.sub(r"(?<!\w)\+?\d[\d -]{8,}\d(?!\w)","[号码已隐去]",v)
    for token, reference in references.items():
        v = v.replace(token, reference)
    return v
