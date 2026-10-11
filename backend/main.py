import csv
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field
from .config import DATA_DIR, ROOT, MODEL
from .engine import Engine
from .store import Store, now
from .integrations import IntegrationError, integration_status, test_connection, shopify_readonly

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

class TaskRequest(BaseModel):
    command: str = Field(min_length=1, max_length=2000)
    kind: Literal["auto", "orders", "ads", "incident", "memory", "purchase", "refund", "chat"] = "auto"
    use_ai: bool = False
    params: dict = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=100)

class Decision(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=500)

class Toggle(BaseModel):
    enabled: bool

class BotRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    sender_id: str = Field(min_length=1, max_length=100)
    command: str = Field(min_length=1, max_length=2000)

def create_app(directory: Path | None = None):
    store = Store(directory or DATA_DIR)
    engine = Engine(store)
    owner_token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    token_path = store.directory / "service.token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    service_token = token_path.read_text(encoding="utf-8").strip()

    @asynccontextmanager
    async def lifespan(app):
        engine.start()
        yield
        engine.close()

    app = FastAPI(title="小跨 AI 运营官", version="0.1.0", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.store, app.state.engine = store, engine
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def owner_session(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in ("/api/health", "/api/bootstrap"):
            bot_call = path == "/api/bot/tasks" and secrets.compare_digest(request.headers.get("x-xiaokua-service", ""), service_token)
            if not bot_call:
                if not secrets.compare_digest(request.cookies.get("xiaokua_owner", ""), owner_token):
                    return JSONResponse({"detail": "请从本机工作台建立会话"}, status_code=401)
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    origin = request.headers.get("origin", "")
                    if origin and origin not in ("http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173"):
                        return JSONResponse({"detail": "不允许跨站写入"}, status_code=403)
                    if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf):
                        return JSONResponse({"detail": "会话校验失败，请刷新页面"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        return {"ok": True, "mode": "simulation", "version": "0.1.0"}

    @app.get("/api/bootstrap")
    def bootstrap(response: Response):
        response.set_cookie("xiaokua_owner", owner_token, httponly=True, samesite="strict", path="/")
        return {"csrf": csrf, "mode": "simulation", "model": MODEL}

    @app.get("/api/state")
    def state():
        tasks = [store.task(r["id"]) for r in store.rows("SELECT id FROM tasks ORDER BY created_at DESC,rowid DESC LIMIT 60")]
        orders = store.rows("SELECT * FROM orders ORDER BY created_at DESC")
        campaigns = store.rows("SELECT * FROM campaigns")
        memories = store.rows("SELECT * FROM memories")
        for memory in memories:
            memory["evidence"] = json.loads(memory["evidence"])
        actions = store.actions()
        ledger = store.rows("SELECT * FROM ledger ORDER BY created_at DESC,rowid DESC")
        spent = sum(row["amount_cents"] for row in ledger if row["kind"] == "purchase")
        today = now()[:10]
        return {"mode": "simulation", "tasks": tasks, "orders": orders, "campaigns": campaigns,
            "inventory": store.rows("SELECT *,stock-reserved AS available FROM inventory"),
            "inventory_snapshot": store.setting("inventory_snapshot"),
            "memories": memories, "skills": store.rows("SELECT * FROM skills"), "actions": actions, "ledger": ledger,
            "audit": store.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 150"), "audit_verification": store.verify_audit(),
            "policy": store.setting("policy"), "scheduler_enabled": store.setting("scheduler_enabled"),
            "wallet": {"balance_cents": store.setting("wallet_initial_cents") - spent, "spent_cents": spent,
                "purchase_today_cents": sum(r["amount_cents"] for r in ledger if r["kind"] == "purchase" and r["created_at"].startswith(today)),
                "refund_today_cents": sum(r["amount_cents"] for r in ledger if r["kind"] == "refund" and r["created_at"].startswith(today))},
            "integrations": integration_status(), "ticket_count": store.one("SELECT COUNT(*) AS total FROM tickets")["total"],
            "metrics": {"revenue_cents": sum(o["total_cents"] for o in orders), "pending_orders": sum(o["status"] == "pending" for o in orders),
                "pending_approvals": sum(a["status"] == "pending" for a in actions),
                "roas": round(sum(c["revenue_cents"] for c in campaigns) / sum(c["spend_cents"] for c in campaigns), 2)},
            "trend": [{"day": day, "DE": de, "FR": fr, "GB": gb, "US": us} for day, de, fr, gb, us in [
                ("周一", 410, 320, 300, 550), ("周二", 520, 370, 290, 610), ("周三", 470, 400, 270, 580),
                ("周四", 660, 470, 230, 710), ("周五", 610, 490, 240, 740), ("周六", 740, 560, 210, 850), ("周日", 820, 620, 190, 940)]]}

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskRequest):
        try:
            return engine.submit(body.command, body.kind, body.use_ai, body.params, body.idempotency_key)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        value = store.task(task_id)
        if not value:
            raise HTTPException(404, "任务不存在")
        return value

    @app.post("/api/tasks/{task_id}/retry")
    def retry_task(task_id: str):
        try:
            return engine.retry(task_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/api/tasks/{task_id}/report")
    def report(task_id: str):
        value = task(task_id)
        body = f"# 小跨运营报告\n\n任务：{value['command']}\n\n状态：{value['status']}\n\n数据来源：模拟业务数据\n\n{value['result'].get('summary', '任务仍在执行')}\n"
        for r in value["result"].get("reports", []):
            body += f"\n## {r['role']}\n\n{r['finding']}\n\n建议：{r['recommendation']}\n\n证据：{', '.join(r['evidence'])}\n"
        body += "\n## 操作结果\n"
        for action in value["actions"]:
            body += f"\n- {action['title']}：{action['status']}（{action['reason']}）"
        return PlainTextResponse(body, headers={"Content-Disposition": f'attachment; filename="{task_id}.md"'})

    @app.post("/api/approvals/{action_id}")
    def approve(action_id: str, body: Decision):
        try:
            return engine.decide(action_id, body.approved, body.note)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/api/skills/{skill_id}/toggle")
    def toggle_skill(skill_id: str, body: Toggle):
        with store.transaction() as db:
            if not db.execute("SELECT 1 FROM skills WHERE id=?", (skill_id,)).fetchone():
                raise HTTPException(404, "技能不存在")
            db.execute("UPDATE skills SET active=? WHERE id=?", (int(body.enabled), skill_id))
            store.audit(db, "skill.toggled", skill_id, {"active": body.enabled}, "负责人")
        return {"ok": True}

    @app.get("/api/skills/{skill_id}/download")
    def download_skill(skill_id: str):
        skill = store.one("SELECT * FROM skills WHERE id=?", (skill_id,))
        if not skill:
            raise HTTPException(404, "技能不存在")
        return PlainTextResponse(skill["content"], headers={"Content-Disposition": f'attachment; filename="{skill_id}-SKILL.md"'})

    @app.post("/api/scheduler")
    def scheduler(body: Toggle):
        with store.transaction() as db:
            store.set_setting("scheduler_enabled", body.enabled, db)
            store.audit(db, "schedule.changed", "morning", {"enabled": body.enabled, "timezone": "Asia/Shanghai", "hour": 9}, "负责人")
        return {"ok": True}

    @app.post("/api/integrations/{name}/test")
    def test_integration(name: str):
        try:
            result = test_connection(name)
        except IntegrationError as error:
            result = {"ok": False, "message": str(error)}
        with store.transaction() as db:
            store.audit(db, "integration.tested", name, result, "负责人")
        return result

    @app.get("/api/integrations/shopify/orders")
    def live_orders():
        try:
            return {"mode": "live-readonly", "data": shopify_readonly("orders")}
        except IntegrationError as error:
            raise HTTPException(400, str(error)) from None

    @app.get("/api/audit/export")
    def audit_export():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["ID", "Time", "Actor", "Event", "Target", "Detail", "Previous Hash", "Hash"])
        def safe(value):
            text = str(value)
            return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text
        for row in store.rows("SELECT * FROM audit ORDER BY id"):
            writer.writerow([safe(row[k]) for k in ("id", "created_at", "actor", "event", "target", "detail", "previous_hash", "hash")])
        return Response("\ufeff" + buffer.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="xiaokua-audit.csv"'})

    @app.post("/api/bot/tasks")
    def bot_task(body: BotRequest, request: Request):
        if not secrets.compare_digest(request.headers.get("x-xiaokua-service", ""), service_token):
            raise HTTPException(403, "机器人服务认证失败")
        allowlist = {v.strip() for v in os.getenv("FEISHU_ALLOWED_OPEN_IDS", "").split(",") if v.strip()}
        if body.sender_id not in allowlist:
            raise HTTPException(403, "该飞书操作者未被授权")
        created = engine.submit(body.command, use_ai=True, idem=f"feishu-{body.event_id}", actor="飞书白名单操作者")
        return {"task_id": created["id"], "message": f"已接收模拟运营任务 {created['id']}。在本机工作台查看执行与审批结果。"}

    dist = ROOT / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}")
    def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "接口不存在")
        index = dist / "index.html"
        if not index.exists():
            return PlainTextResponse("请先在 frontend 目录执行 npm run build", status_code=503)
        return FileResponse(index)

    return app

app = create_app()
