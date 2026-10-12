"""Three continuous merchant journeys using real test-platform events.

This runner does not fabricate Feishu callbacks, place orders with orderCreate,
approve writes, or replace model responses. A browser/user performs checkout,
messages and approval. Each verification gate reads durable records and Shopify.
All raw identifiers, accounts and screenshots remain under ignored data/.
"""
import argparse
import json
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse, unquote

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.config import credentials
from backend.production.api import create_app, build_services
from backend.production.database import connections, events, tasks, actions, jobs, audits, users, resources, knowledge, row
from backend.production.security import Settings

CASES = {
    "fulfillment": {"title": "德国买家下单、履约与物流更新", "market": "DE"},
    "aftersales": {"title": "法国买家下单后全额测试退款、取消与库存恢复", "market": "FR"},
    "knowledge": {"title": "德国买家咨询、技能沉淀复用与商品说明改进", "market": "DE"},
}
REQUIRED = {
    "fulfillment": ["buyer_checkout", "checkout_inventory_decrement", "F-sync", "F-analysis", "F-analysis-report",
                    "F-fulfill-proposal", "F-fulfill-result", "F-shipment-sync", "F-tracking-proposal", "F-tracking-result", "F-close-sync"],
    "aftersales": ["buyer_checkout", "checkout_inventory_decrement", "A-sync", "A-analysis", "A-analysis-report",
                   "A-refund-proposal", "A-refund-result", "A-cancel-proposal", "A-cancel-result", "A-close-sync"],
    "knowledge": ["buyer_checkout", "checkout_inventory_decrement", "C-sync", "C-baseline", "C-baseline-report",
                  "support-learning", "C-reuse", "C-reuse-report", "C-unsafe-proposal", "C-unsafe-result",
                  "C-description-proposal", "C-description-result", "C-frontpage", "C-fulfill-proposal", "C-fulfill-result", "C-close-sync"],
}


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def settings(directory, port=8005, worker=True):
    return Settings(directory, "sqlite:///" + (directory / "product.sqlite").as_posix(),
                    public_url=f"http://127.0.0.1:{port}", worker_enabled=worker, test_only=True)


def load(context_path):
    path = Path(context_path).resolve()
    if not path.is_relative_to((ROOT / "data").resolve()) or path.name != "context.json":
        raise ValueError("Use a private data/journey-*/context.json")
    context = json.loads(path.read_text(encoding="utf-8"))
    if context["test_only"] is not True:
        raise ValueError("Test protection is required")
    directory = path.parent
    return path, context, build_services(settings(directory, context["port"]))


def initialize(recipient_path, port):
    key = credentials()
    recipient = json.loads(Path(recipient_path).read_text(encoding="utf-8"))
    if recipient["app_id"] != key.get("FEISHU_APP_ID") or not recipient["open_id"].startswith("ou_"):
        raise ValueError("Use the previously verified recipient for your own Feishu application")
    directory = ROOT / "data" / ("journey-" + secrets.token_hex(6))
    directory.mkdir()
    context = {"schema": 1, "created_at": now(), "port": port, "test_only": True,
               "real_money_used": False, "cases": {}, "setup": [], "evidence": []}
    accounts = {"email": "journey-owner@example.invalid", "password": secrets.token_urlsafe(24),
                "approval_email": "journey-approver@example.invalid", "approval_password": secrets.token_urlsafe(24)}
    write(directory / "accounts.json", accounts)
    app = create_app(settings(directory, port, False))
    with TestClient(app) as client:
        def post(route, body):
            csrf = client.get("/api/bootstrap").json()["csrf"]
            response = client.post("/api" + route, json=body, headers={"X-CSRF-Token": csrf})
            if response.status_code >= 400:
                raise RuntimeError("Setup API failed: " + route + " / " + str(response.status_code))
            return response.json()
        owner = post("/auth/register", dict(email=accounts["email"], password=accounts["password"],
            name="案例商家负责人", organization="电商用户旅程研究", setup_token=(directory / "setup.token").read_text()))["user"]
        context.update(tenant_id=owner["tenant_id"], owner_id=owner["id"])
        context["setup"].append({"step": "fresh_organization_registered", "passed": True})
        shop_id = post("/shopify/import-local", {})["id"]
        api = app.state.ops.client(shop_id, owner["tenant_id"])
        cap = api.capability()
        if not cap["shop"]["plan"]["partnerDevelopment"]:
            raise RuntimeError("A Shopify development shop is required")
        # Check permissions before creating anything. No customer identity fields are queried.
        api.orders("test:true")
        context["shopify_connection"] = shop_id
        context["shop_domain"] = cap["shop"]["myshopifyDomain"]
        context["setup"].append({"step": "real_development_shop_connected", "passed": True})
        context["feishu_connection"] = post("/feishu/connect", {
            "app_id": key["FEISHU_APP_ID"], "app_secret": key["FEISHU_APP_SECRET"], "delivery": "websocket"})["id"]
        invite = post("/members/invite", {"email": accounts["approval_email"], "role": "approver"})["url"].split("invite=")[1]
        with TestClient(app) as approver_client:
            csrf = approver_client.get("/api/bootstrap").json()["csrf"]
            accepted = approver_client.post("/api/auth/invite/accept", json={
                "email": accounts["approval_email"], "password": accounts["approval_password"],
                "name": "案例独立审批人", "token": invite}, headers={"X-CSRF-Token": csrf})
            if accepted.status_code != 201:
                raise RuntimeError("Independent member invitation failed")
            context["approver_id"] = accepted.json()["user"]["id"]
        post("/members/" + context["approver_id"], {"role": "approver", "active": True, "feishu_open_id": recipient["open_id"]})
        context["setup"].append({"step": "real_feishu_bot_and_independent_member_bound", "passed": True})
        primary = api.gql("{location{id isActive fulfillsOnlineOrders shipsInventory}} ")["location"]
        if not primary or not all(primary[k] for k in ("isActive", "fulfillsOnlineOrders", "shipsInventory")):
            raise RuntimeError("Configure shipping at the primary location before initializing checkout fixtures")
        location = primary["id"]
        path = directory / "context.json"
        write(path, context)
        for case, definition in CASES.items():
            tag = directory.name + "-" + case
            product = api.mutation('''mutation($product:ProductCreateInput!){productCreate(product:$product){product{id handle variants(first:1){nodes{id inventoryItem{id}}}} userErrors{field message}}}''',
                {"product": {"title": "小跨 E2E " + case + " " + directory.name[-6:], "status": "DRAFT",
                    "tags": ["xiaokua-test", tag], "descriptionHtml": "<p>智能灯泡研究样品。仅供测试网关使用；能效等级待核对，退货政策由商家确认。</p>"}}, "productCreate")["product"]
            variant = product["variants"]["nodes"][0]
            data = dict(definition, product_id=product["id"], variant_id=variant["id"],
                        item_id=variant["inventoryItem"]["id"], location_id=location, tag=tag,
                        handle=product["handle"], initial_stock=10, steps=[], refs={})
            context["cases"][case] = data
            write(path, context)  # Persist each resource before another mutation.
            api.mutation('''mutation($id:ID!,$input:InventoryItemInput!){inventoryItemUpdate(id:$id,input:$input){inventoryItem{id} userErrors{field message}}}''',
                {"id": data["item_id"], "input": {"tracked": True, "sku": "XK-E2E-" + case.upper()}}, "inventoryItemUpdate")
            api.mutation('''mutation($product:ID!,$variants:[ProductVariantsBulkInput!]!){productVariantsBulkUpdate(productId:$product,variants:$variants){productVariants{id price} userErrors{field message}}}''',
                {"product": product["id"], "variants": [{"id": variant["id"], "price": "19.90", "taxable": False}]}, "productVariantsBulkUpdate")
            api.mutation('''mutation($item:ID!,$loc:ID!,$key:String!){inventoryActivate(inventoryItemId:$item,locationId:$loc,available:10) @idempotent(key:$key){inventoryLevel{id} userErrors{field message}}}''',
                {"item": data["item_id"], "loc": location, "key": tag + "-stock"}, "inventoryActivate")
            data["stock_verified"] = api.inventory(data["item_id"], location)["inventoryLevel"]["quantities"][0]["quantity"] == 10
            write(path, context)
    print(json.dumps({"context": path.relative_to(ROOT).as_posix(), "test_only": True, "orders_created": 0, "messages_sent": 0}, ensure_ascii=False))


@contextmanager
def client(context, directory):
    account = json.loads((directory / "accounts.json").read_text())
    with httpx.Client(base_url=f"http://127.0.0.1:{context['port']}", trust_env=False, timeout=40) as c:
        cache = directory / "verifier-session.json"
        authenticated = False
        if cache.is_file():
            for cookie in json.loads(cache.read_text()):
                c.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie["path"])
            user = c.get("/api/bootstrap").json().get("user")
            authenticated = bool(user and user["id"] == context.get("owner_id"))
        if not authenticated:
            post(c, "/auth/login", {"email": account["email"], "password": account["password"]})
        write(cache, [{"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path} for cookie in c.cookies.jar])
        yield c


def post(c, route, body):
    csrf = c.get("/api/bootstrap").json()["csrf"]
    response = c.post("/api" + route, json=body, headers={"X-CSRF-Token": csrf})
    if response.status_code >= 400:
        raise RuntimeError("Journey API failed: " + route + " / " + str(response.status_code))
    return response.json()


def gate(context, case, step, passed, source, **facts):
    entry = {"step": step, "passed": bool(passed), "source": source, "observed_at": now(), "facts": facts}
    context.setdefault("evidence", []).append(dict(entry, case=case))
    record = context["cases"][case]
    record["steps"] = [s for s in record["steps"] if s["step"] != step] + [entry]
    return bool(passed)


def bind_checkout(path, context, services, case):
    db, vault, ops, feishu, s = services
    fixture = context["cases"][case]
    api = ops.client(context["shopify_connection"], context["tenant_id"])
    # Source provenance comes from Shopify, not an operator-supplied 'passed' flag.
    orders = api.gql('''query{orders(first:50,sortKey:CREATED_AT,reverse:true,query:"test:true"){nodes{id name sourceName test createdAt lineItems(first:100){nodes{variant{id} quantity}} transactions{id test gateway status kind} totalPriceSet{shopMoney{amount currencyCode}}}}}''')["orders"]["nodes"]
    matching = [o for o in orders if any((line.get("variant") or {}).get("id") == fixture["variant_id"] for line in o["lineItems"]["nodes"])]
    if len(matching) != 1:
        raise RuntimeError("Expected exactly one checkout order for this new case product")
    order = matching[0]
    paid = [t for t in order["transactions"] if t["kind"] in ("SALE", "CAPTURE") and t["status"] == "SUCCESS"]
    valid = order["sourceName"] == "web" and order["test"] and paid and all(t["test"] and t["gateway"] == "bogus" for t in paid)
    fixture["order_id"] = order["id"]
    fixture["order_name"] = order["name"]
    fixture["checkout_observation"] = order
    stock = api.inventory(fixture["item_id"], fixture["location_id"])["inventoryLevel"]["quantities"][0]["quantity"]
    fixture["stock_after_checkout"] = stock
    gate(context, case, "buyer_checkout", valid, "Shopify order + browser checkout", source_name=order["sourceName"], test=True, successful_test_transaction=bool(paid))
    gate(context, case, "checkout_inventory_decrement", stock == 9, "Shopify inventory readback", before=10, after=stock)
    write(path, context)
    if not valid or stock != 9:
        raise RuntimeError("Checkout provenance or inventory gate failed")
    print(json.dumps({"case": case, "checkout": "passed", "stock": stock, "real_money_used": False}))


def verify_message(path, context, services, case, kind, marker, step_name=None):
    db = services[0]
    tenant = context["tenant_id"]
    f = context["cases"][case]
    step_name = step_name or marker
    with db.engine.connect() as conn:
        received = [row(r) for r in conn.execute(select(events).where(
            events.c.connection_id == context["feishu_connection"], events.c.topic == "im.message.receive_v1"))
            if marker in r._mapping["payload"].get("text", "")]
    if len(received) != 1:
        raise RuntimeError("Exactly one real SDK message with this marker is required")
    ev = received[0]
    matches = [t for t in db.list(tasks, tenant, 10000) if t["idem"] == "feishu-" + ev["external_id"]]
    if len(matches) != 1:
        raise RuntimeError("Message has not created exactly one durable task")
    task = matches[0]
    f["refs"][marker] = {"event_id": ev["id"], "task_id": task["id"]}
    posted = [j for j in db.list(jobs, tenant, 10000) if j["kind"] == "notify_task" and j["target_id"] == task["id"]]
    base = ev["status"] == "processed" and task["status"] == "completed" and len(posted) == 1 and posted[0]["status"] == "done"
    ok = False
    if kind == "sync":
        local = db.list(resources, tenant, 1, kind="order", external_id=f["order_id"])
        ok = base and task["kind"] == "sync" and bool(local) and bool(local[0]["data"].get("lineItems"))
        gate(context, case, step_name, ok, "Real Feishu SDK event → durable worker → Shopify snapshot → Feishu result", case_order_present=bool(local), result_notification_sent=bool(posted and posted[0]["status"] == "done"))
    elif kind == "analyze":
        result = task["result"]
        reports = result.get("reports", [])
        scoped = result.get("statistics", {}).get("scope", {})
        order_role = next((r for r in reports if r["role_key"] == "orders"), {})
        inventory_role = next((r for r in reports if r["role_key"] == "inventory"), {})
        ok = base and len(reports) == 3 and all(r["mode"] == "deepseek" for r in reports)
        ok = ok and scoped.get("type") == "explicit_orders" and scoped.get("order_names") == [f["order_name"]]
        ok = ok and order_role.get("evidence") == [f["order_id"]] and inventory_role.get("evidence") == [f["variant_id"]]
        if case == "knowledge" and step_name == "C-reuse":
            role = next((r for r in reports if r["role_key"] == "knowledge"), {})
            selected = result.get("statistics", {}).get("knowledge", [])
            ok = ok and len(selected) == 1 and selected[0]["market"] == "DE" and role.get("evidence") == [selected[0]["id"]]
        gate(context, case, step_name, ok, "Real SDK message → real DeepSeek roles → validated case evidence → Feishu result", model_roles=[r["mode"] for r in reports], explicit_order_scope=scoped.get("type") == "explicit_orders", scoped_order_count=result.get("statistics", {}).get("order_count"), result_notification_sent=bool(posted and posted[0]["status"] == "done"))
        with client(context, path.parent) as c:
            exported = c.get("/api/tasks/" + task["id"] + "/report")
            (path.parent / (marker + "-report.md")).write_text(exported.text, encoding="utf-8")
            ok = gate(context, case, step_name + "-report", exported.status_code == 200 and "deepseek" in exported.text, "Authenticated report export") and ok
    write(path, context)
    print(json.dumps({"case": case, "gate": marker, "passed": bool(ok), "task_status": task["status"]}, ensure_ascii=False))
    if not ok:
        raise RuntimeError("Real message / result / model / scope gate has not passed")


def selected_action(db, context, case, kind, label):
    f = context["cases"][case]
    if label in f["refs"]:
        return db.get(actions, f["refs"][label]["action_id"], context["tenant_id"])
    matches = [a for a in db.list(actions, context["tenant_id"], 10000) if a["kind"] == kind
        and a["actor_id"] == context["owner_id"]
        and ((a["payload"].get("order_id") == f.get("order_id")) if kind != "product" else a["payload"].get("product_id") == f["product_id"])
        and a["id"] not in {v.get("action_id") for v in f["refs"].values()}]
    if len(matches) != 1:
        raise RuntimeError("Expected exactly one new case-specific proposal")
    return matches[0]


def verify_proposal(path, context, services, case, kind, label):
    db, vault, ops, feishu, s = services
    f = context["cases"][case]
    a = selected_action(db, context, case, kind, label)
    f["refs"][label] = {"task_id": a["task_id"], "action_id": a["id"]}
    sent = [j for j in db.list(jobs, context["tenant_id"], 10000) if j["kind"] == "notify" and j["target_id"] == a["id"]]
    api = ops.client(context["shopify_connection"], context["tenant_id"])
    if kind == "product":
        unchanged = api.product(f["product_id"])["descriptionHtml"] == a["before"]["description_html"]
    else:
        from backend.production.workflows import order_snapshot
        observed = order_snapshot(api.order(f["order_id"]))
        unchanged = all(observed.get(k) == value for k, value in a["before"].items() if k != "tracking")
    with client(context, path.parent) as c:
        csrf = c.get("/api/bootstrap").json()["csrf"]
        denied = c.post("/api/approvals/" + a["id"], json={"approved": True}, headers={"X-CSRF-Token": csrf})
    ok = a["status"] == "pending" and unchanged and denied.status_code == 422 and len(sent) == 1 and sent[0]["status"] == "done"
    gate(context, case, label + "-proposal", ok, "Workbench proposal + platform readback + real approval-card delivery", waiting_for_approval=a["status"] == "pending", platform_unchanged=unchanged, applicant_self_approval_denied=denied.status_code == 422)
    write(path, context)
    print(json.dumps({"case": case, "gate": label + "-proposal", "passed": bool(ok)}))
    if not ok:
        raise RuntimeError("Proposal protection or real card delivery gate failed")


def verify_action(path, context, services, case, kind, label, rejected=False):
    db, vault, ops, feishu, s = services
    f = context["cases"][case]
    a = selected_action(db, context, case, kind, label)
    with db.engine.connect() as conn:
        callbacks = [row(r) for r in conn.execute(select(events).where(
            events.c.connection_id == context["feishu_connection"], events.c.topic == "card.action.trigger"))
            if r._mapping["payload"].get("value", {}).get("action_id") == a["id"]]
    decision = "reject" if rejected else "approve"
    callback = any(e["status"] == "processed" and e["payload"]["value"].get("decision") == decision for e in callbacks)
    sent = [j for j in db.list(jobs, context["tenant_id"], 10000) if j["kind"] == "notify_result" and j["target_id"] == a["id"]]
    expected = "rejected" if rejected else "executed"
    ok = callback and a["approved_by"] == context["approver_id"] and a["approved_by"] != a["actor_id"] and a["status"] == expected and len(sent) == 1 and sent[0]["status"] == "done"
    api = ops.client(context["shopify_connection"], context["tenant_id"])
    facts = {"independent_approval": a["approved_by"] != a["actor_id"], "real_callback": callback, "status": a["status"]}
    if kind == "product":
        product = api.product(f["product_id"])
        matches = product["descriptionHtml"] == (a["before"]["description_html"] if rejected else a["payload"]["description_html"])
        facts["description_matches"] = matches
    else:
        order = api.order(f["order_id"])
        if kind in ("fulfill", "tracking"):
            matches = order["displayFulfillmentStatus"] == "FULFILLED" and any(t["number"] == a["payload"]["number"] for item in order["fulfillments"] for t in item["trackingInfo"])
            facts["fulfillment_status"] = order["displayFulfillmentStatus"]
            facts["tracking_matches"] = matches
        elif kind == "refund":
            from backend.production.workflows import cents_nonnegative
            before = cents_nonnegative(a["before"]["totalRefundedSet"]["shopMoney"]["amount"])
            current = cents_nonnegative(order["totalRefundedSet"]["shopMoney"]["amount"])
            matches = current - before == a["payload"]["amount_cents"] and all(t["test"] for t in order["transactions"])
            facts["refunded_amount"] = order["totalRefundedSet"]["shopMoney"]["amount"]
            facts["net_payment"] = order["netPaymentSet"]["shopMoney"]["amount"]
        else:
            stock = api.inventory(f["item_id"], f["location_id"])["inventoryLevel"]["quantities"][0]["quantity"]
            matches = bool(order["cancelledAt"]) and stock == f["initial_stock"] and order["totalRefundedSet"] == a["before"]["totalRefundedSet"]
            facts.update(cancelled=bool(order["cancelledAt"]), restored_inventory=stock, no_implicit_extra_refund=order["totalRefundedSet"] == a["before"]["totalRefundedSet"])
        f["final_order_observation"] = order
    ok = bool(ok and matches)
    gate(context, case, label + "-result", ok, "Real Feishu callback → independent decision → worker → Shopify readback → result notice", **facts)
    write(path, context)
    print(json.dumps({"case": case, "gate": label + "-result", "passed": ok, "status": a["status"]}))
    if not ok:
        raise RuntimeError("Real approval / execution / platform result gate has not passed")


def verify_support(path, context, services):
    db = services[0]
    tenant = context["tenant_id"]
    tickets = db.list(resources, tenant, 100, kind="ticket")
    entries = db.list(knowledge, tenant, 100)
    german = [k for k in entries if k["market"] == "DE" and "能源效率" in k["title"]]
    learned = db.list(audits, tenant, 10000, event="knowledge.learned")
    inactive_drafts = len(learned) == 2 and all(entry["detail"].get("active") is False for entry in learned)
    ok = len(tickets) == 10 and len(entries) == 2 and len(german) == 1 and all(k["active"] for k in entries) and len(german[0]["evidence"]) == 5 and inactive_drafts
    with client(context, path.parent) as c:
        package = c.get("/api/knowledge/" + german[0]["id"] + "/bundle") if german else None
    ok = ok and package is not None and package.status_code == 200
    gate(context, "knowledge", "support-learning", ok, "UI support import → evidence drafts → operator review/enable → skill bundle export", support_records=len(tickets), evidence_drafts=len(entries), drafts_initially_inactive=inactive_drafts, de_skill_active=bool(german and german[0]["active"]), both_market_skills_active=all(k["active"] for k in entries), evidence_count=len(german[0]["evidence"]) if german else 0)
    write(path, context)
    print(json.dumps({"case": "knowledge", "gate": "support-learning", "passed": bool(ok)}))
    if not ok:
        raise RuntimeError("Support learning and export gate failed")


def verify_frontpage(path, context, services):
    f = context["cases"]["knowledge"]
    evidence = json.loads((path.parent / "frontend-evidence.json").read_text(encoding="utf-8"))
    url = urlparse(evidence["source_url"])
    screenshot = (path.parent / evidence["screenshot_file"]).resolve()
    if not screenshot.is_relative_to(path.parent) or not screenshot.is_file():
        raise RuntimeError("Storefront screenshot must exist in the private case directory")
    product = services[2].client(context["shopify_connection"], context["tenant_id"]).product(f["product_id"])
    import html, re
    matches = len(evidence["description_text"]) > 20 and evidence["description_text"] in html.unescape(re.sub(r"<[^>]*>", "", product["descriptionHtml"]))
    ok = url.scheme == "https" and url.hostname == context["shop_domain"] and unquote(url.path) == "/products/" + f["handle"] and matches
    gate(context, "knowledge", "C-frontpage", ok, "Browser rendered product DOM + private screenshot + approved Shopify description", browser_description_matches=matches)
    write(path, context)
    print(json.dumps({"case":"knowledge", "gate":"C-frontpage", "passed":bool(ok)}))
    if not ok:
        raise RuntimeError("Storefront readback does not match this case product")


def finish(path, context, services, output):
    db, vault, ops, feishu, s = services
    api = ops.client(context["shopify_connection"], context["tenant_id"])
    all_ok = True
    for case, f in context["cases"].items():
        completed = {step["step"] for step in f["steps"] if step["passed"]}
        missing = [step for step in REQUIRED[case] if step not in completed]
        order = api.order(f["order_id"]) if f.get("order_id") else None
        quantity = api.inventory(f["item_id"], f["location_id"])["inventoryLevel"]["quantities"][0]["quantity"]
        final = bool(order and order["test"] and all(t["test"] for t in order["transactions"]))
        if case == "aftersales":
            final = final and bool(order["cancelledAt"]) and Decimal(order["netPaymentSet"]["shopMoney"]["amount"]) == 0 and quantity == 10
        else:
            final = final and order["displayFulfillmentStatus"] == "FULFILLED" and quantity == 9
        # The last snapshot is refreshed through a real Feishu-originated sync.
        local = db.list(resources, context["tenant_id"], 1, kind="order", external_id=f.get("order_id", ""))
        final = final and bool(local) and local[0]["data"]["displayFulfillmentStatus"] == order["displayFulfillmentStatus"] and local[0]["data"]["totalRefundedSet"] == order["totalRefundedSet"]
        valid_audit = db.verify(context["tenant_id"])["valid"]
        with client(context, path.parent) as c:
            exported = c.get("/api/audit/export")
            (path.parent / (case + "-audit.csv")).write_bytes(exported.content)
        related = [a for a in db.list(actions, context["tenant_id"], 10000) if a["payload"].get("order_id") == f.get("order_id") or a["payload"].get("product_id") == f["product_id"]]
        unresolved = [a for a in related if a["status"] not in ("executed", "rejected")]
        clean_jobs = all(j["status"] == "done" for j in db.list(jobs, context["tenant_id"], 10000))
        ok = not missing and final and valid_audit and exported.status_code == 200 and not unresolved and clean_jobs
        gate(context, case, "closed-business-outcome", bool(ok), "Linked case order, final platform/local state, immutable audit and exports", required_steps_present=not missing, final_outcome_verified=bool(final), audit_valid=valid_audit, no_unresolved_actions=not unresolved, all_jobs_completed=clean_jobs)
        f["status"] = "passed" if ok else "incomplete"
        f["missing_steps"] = missing
        all_ok = all_ok and ok
    context["completed_at"] = now()
    write(path, context)
    # Strict allowlist: no private IDs, domain, accounts, prompts, model text or
    # raw platform payloads are copied into a public artifact.
    summary = {"schema": 1, "captured_at": now(), "scope": "Real development-platform merchant journeys with synthetic buyers",
        "real_shopify": True, "real_feishu_sdk": True, "real_model": True, "real_money_used": False,
        "checkout_source": "browser + Shopify test payment gateway", "passed": bool(all_ok), "cases": []}
    safe_facts = {"source_name", "test", "successful_test_transaction", "before", "after", "case_order_present", "result_notification_sent",
        "model_roles", "explicit_order_scope", "scoped_order_count", "waiting_for_approval", "platform_unchanged", "applicant_self_approval_denied",
        "independent_approval", "real_callback", "status", "description_matches", "fulfillment_status", "tracking_matches",
        "refunded_amount", "net_payment", "cancelled", "restored_inventory", "no_implicit_extra_refund", "support_records", "evidence_drafts",
        "de_skill_active", "evidence_count", "drafts_initially_inactive", "both_market_skills_active", "browser_description_matches", "required_steps_present", "final_outcome_verified", "audit_valid", "no_unresolved_actions", "all_jobs_completed"}
    for case, definition in CASES.items():
        f = context["cases"][case]
        by_step = {step["step"]:step for step in f["steps"]}
        ordered_steps = [by_step[key] for key in REQUIRED[case] + ["closed-business-outcome"] if key in by_step]
        summary["cases"].append({"id": case, "title": definition["title"], "market": definition["market"], "status": f["status"],
            "steps": [{"step": step["step"], "passed": step["passed"], "observed_at": step["observed_at"],
                       "facts": {k:v for k,v in step["facts"].items() if k in safe_facts}}
                      for step in ordered_steps]})
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        write(output, summary)
    print(json.dumps({"journeys": [{"id": r["id"], "status": r["status"]} for r in summary["cases"]], "passed": bool(all_ok)}, ensure_ascii=False))
    if not all_ok:
        raise RuntimeError("An incomplete journey cannot be reported as passed")


def serve(path, context, mode):
    directory = path.parent
    if mode == "api":
        import uvicorn
        uvicorn.run(create_app(settings(directory, context["port"])), host="127.0.0.1", port=context["port"], access_log=False, log_level="error")
    else:
        import lark_oapi as lark
        from backend.production.feishu_ws import build_handler
        db, vault, ops, feishu, s = build_services(settings(directory, context["port"]))
        con = db.get(connections, context["feishu_connection"], context["tenant_id"])
        secret = vault.open(con["secret"])
        handler = build_handler(db, feishu, con, secret["app_id"])
        ws = lark.ws.Client(secret["app_id"], secret["app_secret"], event_handler=handler, log_level=lark.LogLevel.ERROR)
        print("Real authenticated journey SDK starting; payloads and credentials are not logged.", flush=True)
        try:
            ws.start()
        finally:
            db.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--recipient-file", required=True, type=Path)
    init.add_argument("--port", type=int, default=8005)
    server = sub.add_parser("serve")
    server.add_argument("mode", choices=["api", "ws"])
    server.add_argument("--context", required=True, type=Path)
    checkout = sub.add_parser("checkout")
    checkout.add_argument("case", choices=CASES)
    checkout.add_argument("--context", required=True, type=Path)
    message = sub.add_parser("message")
    message.add_argument("case", choices=CASES)
    message.add_argument("kind", choices=["sync", "analyze"])
    message.add_argument("marker")
    message.add_argument("--step", help="Canonical gate name when retesting after a fix")
    message.add_argument("--context", required=True, type=Path)
    for command in ("proposal", "action"):
        p = sub.add_parser(command)
        p.add_argument("case", choices=CASES)
        p.add_argument("kind", choices=["fulfill", "tracking", "refund", "cancel", "product"])
        p.add_argument("label")
        p.add_argument("--context", required=True, type=Path)
        if command == "action":
            p.add_argument("--rejected", action="store_true")
    support = sub.add_parser("support")
    support.add_argument("--context", required=True, type=Path)
    frontpage = sub.add_parser("frontpage")
    frontpage.add_argument("--context", required=True, type=Path)
    final = sub.add_parser("finish")
    final.add_argument("--context", required=True, type=Path)
    final.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        initialize(args.recipient_file, args.port)
    else:
        path, context, services = load(args.context)
        if args.command == "serve":
            services[0].engine.dispose()
            serve(path, context, args.mode)
        elif args.command == "checkout":
            bind_checkout(path, context, services, args.case)
        elif args.command == "message":
            verify_message(path, context, services, args.case, args.kind, args.marker, args.step)
        elif args.command == "proposal":
            verify_proposal(path, context, services, args.case, args.kind, args.label)
        elif args.command == "action":
            verify_action(path, context, services, args.case, args.kind, args.label, args.rejected)
        elif args.command == "support":
            verify_support(path, context, services)
        elif args.command == "frontpage":
            verify_frontpage(path, context, services)
        elif args.command == "finish":
            finish(path, context, services, args.output)
        services[0].engine.dispose()


if __name__ == "__main__":
    main()
