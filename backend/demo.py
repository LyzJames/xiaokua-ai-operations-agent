"""Loopback-only, credential-free research demo using the real workflow and worker."""
import argparse
import json
import uuid
from pathlib import Path
from fastapi import Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select
from .config import ROOT
from .demo_shop import DemoShop
from .production.api import create_app
from .production.security import Settings, password_hash
from .production.database import tenants, users, connections, resources, clock, row
from .production.shopify import upsert_resource

DEMO_PASSWORD = "demo-only-local-123"
SHOP_CONNECTION = "conn_demo_shopify"
FEISHU_CONNECTION = "conn_demo_feishu"
DEMO_ORG = "org_demo_research"


def create_demo(directory=None, port=8003, worker_enabled=True):
    directory = Path(directory or ROOT / "data" / "research_demo")
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "research-demo.marker"
    if (directory / "product.sqlite").exists() and not marker.exists():
        raise RuntimeError("演示入口拒绝使用已有的非演示数据库")
    marker.write_text("Synthetic data only", encoding="utf-8")
    shop = DemoShop(directory / "synthetic-shop.json")
    app = create_app(Settings(directory, "sqlite:///" + str(directory / "product.sqlite"),
        public_url=f"http://127.0.0.1:{port}", worker_enabled=worker_enabled, test_only=True), lambda c, t: shop)
    db, ops, feishu = app.state.db, app.state.ops, app.state.feishu
    app.state.demo = True
    app.state.demo_shop = shop
    if not db.list(tenants):
        with db.transaction() as conn:
            conn.execute(tenants.insert().values(id=DEMO_ORG, name="研究演示组织 · 全部为合成数据", audit_head="0" * 64,
                settings={"sync_enabled": False, "sync_interval_minutes": 30}, created_at=clock()))
            for role, name in (("owner", "演示负责人"), ("approver", "演示审批人"), ("operator", "演示运营员")):
                conn.execute(users.insert().values(id="user_demo_" + role, tenant_id=DEMO_ORG, email=role+"@example.invalid",
                    name=name, role=role, active=True, password=password_hash(DEMO_PASSWORD),
                    feishu_open_id="ou_demo_"+role, created_at=clock()))
            for cid, provider in ((SHOP_CONNECTION, "shopify"), (FEISHU_CONNECTION, "feishu")):
                conn.execute(connections.insert().values(id=cid, tenant_id=DEMO_ORG, provider=provider,
                    external_id="synthetic.myshopify.com" if provider=="shopify" else "cli_demo_synthetic",
                    secret=app.state.vault.seal({"app_id": "cli_demo_synthetic", "app_secret": "demo-offline-placeholder"}),
                    status="active", info={"delivery":"websocket", "synthetic":True}, created_at=clock()))
            db.audit(conn, DEMO_ORG, "demo", "demo.created", DEMO_ORG, {"synthetic": True, "outbound_platform_calls": False})

    # Only this app instance substitutes adapters. Normal backend.service is unchanged.
    def simulated_send(token, open_id, kind, content, key):
        with db.transaction() as conn:
            upsert_resource(conn, DEMO_ORG, FEISHU_CONNECTION, "demo_message",
                {"id": key, "recipient": open_id, "type": kind, "content": content, "synthetic": True})
        return {"code":0, "data":{"message_id":"om_demo_"+key[:20]}}
    feishu.token = lambda secret: "offline-demo-token"
    feishu.send = simulated_send
    real_analyze, real_sync = ops.analyze, ops.sync
    def analyze(task):
        task = dict(task, params=dict(task["params"], use_ai=False))
        result = real_analyze(task)
        result.update(summary="合成数据三角色分析已完成；离线规则模式，不调用大模型。", source="研究演示合成数据")
        return result
    def sync(task):
        result = real_sync(task)
        result.update(summary=f"已同步 {result['orders']} 笔合成订单与 {result['variants']} 个合成商品规格。未访问外部平台。", source="本地 DemoShop 合成适配器")
        return result
    ops.analyze, ops.sync = analyze, sync

    @app.middleware("http")
    async def demo_guard(request, call_next):
        if request.url.path.startswith(("/api/shopify/", "/api/feishu/", "/hooks/")):
            return JSONResponse({"detail":"研究演示禁止真实平台授权、凭据导入和外部回调"}, 403)
        return await call_next(request)

    @app.get("/demo/feishu")
    def console():
        return FileResponse(ROOT / "frontend" / "demo" / "feishu.html")

    @app.get("/demo/feishu.js")
    def console_js():
        return FileResponse(ROOT / "frontend" / "demo" / "feishu.js", media_type="application/javascript")

    @app.get("/api/demo/outbox")
    def outbox(request: Request):
        actor = request.state.identity
        with db.engine.connect() as conn:
            messages = [row(r)["data"] for r in conn.execute(select(resources).where(
                resources.c.tenant_id == DEMO_ORG, resources.c.kind == "demo_message"
            ).order_by(resources.c.updated_at.desc()).limit(100))]
        return {"synthetic":True, "user":actor["name"], "messages":[m for m in messages
            if m["recipient"]==actor["feishu_open_id"]]}

    @app.post("/api/demo/events")
    async def demo_event(request: Request):
        actor = request.state.identity
        body = await request.json()
        topic = "card.action.trigger" if body.get("action_id") else "im.message.receive_v1"
        text = body.get("text", "")
        if not isinstance(text,str) or len(text)>2000: raise HTTPException(422,"演示消息无效")
        value = {"header":{"app_id":"cli_demo_synthetic", "event_id":"demo-"+uuid.uuid4().hex, "event_type":topic}, "event":
            {"operator":{"open_id":actor["feishu_open_id"]}, "action":{"value":{"action_id":body["action_id"],"decision":body.get("decision")}}}
            if topic=="card.action.trigger" else {"sender":{"sender_type":"user","sender_id":{"open_id":actor["feishu_open_id"]}},
                "message":{"message_type":"text","message_id":"demo-message-"+uuid.uuid4().hex,"content":json.dumps({"text":text})}}}
        return dict(feishu.receive(db.get(connections, FEISHU_CONNECTION), value), synthetic=True)
    # The shared app's SPA fallback must follow the demo-specific routes.
    fallback = next(route for route in app.router.routes if getattr(route, "path", None) == "/{path:path}")
    app.router.routes.remove(fallback)
    app.router.routes.append(fallback)
    return app


def main():
    parser = argparse.ArgumentParser(description="小跨离线研究原型 · 不访问真实平台")
    parser.add_argument("--port",type=int,default=8003)
    parser.add_argument("--data-dir",type=Path)
    args = parser.parse_args()
    import uvicorn
    print(f"研究演示：http://127.0.0.1:{args.port}；全部为合成数据，不调用外部平台。",flush=True)
    print("演示账号：owner@example.invalid / approver@example.invalid / operator@example.invalid；演示密码："+DEMO_PASSWORD,flush=True)
    uvicorn.run(create_demo(args.data_dir,args.port), host="127.0.0.1", port=args.port, access_log=False)


if __name__=="__main__": main()
