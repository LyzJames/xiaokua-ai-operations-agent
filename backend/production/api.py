import csv
import io
import json
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlencode, urlparse
from typing import Literal
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select, update, func
from sqlalchemy.exc import IntegrityError
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .database import (Database, tenants, users, sessions, invitations, connections, oauth_states,
                       tasks, actions, resources, audits, jobs, events, knowledge, runtime_workers, inventory_pools, clock, uid, row)
from .security import Settings, Vault, ROLES, digest, password_hash, password_ok, shop_domain, shopify_query_hmac, body_hmac
from .shopify import SCOPES, Shopify, ShopifyError, exchange
from .workflows import Operations, BusinessError, redact
from .feishu import Feishu, FeishuError, tenant_token, feishu_request, verify_event
from .worker import Worker
from ..config import ROOT, credentials
from .platforms.contracts import PlatformError, PROVIDERS, normalize_resource
from .platforms.registry import connection_capabilities
from .commands import submit_command

class AuthBody(BaseModel):
    email:str=Field(min_length=3,max_length=254)
    password:str=Field(min_length=1,max_length=128)
class Signup(AuthBody):
    name:str=Field(min_length=1,max_length=100)
    organization:str=Field(min_length=1,max_length=120)
    setup_token:str=""
class InviteBody(BaseModel):
    email:str=Field(min_length=3,max_length=254)
    role:str="operator"
class AcceptInvite(AuthBody):
    name:str=Field(min_length=1,max_length=100)
    token:str=Field(min_length=10,max_length=100)
class MemberBody(BaseModel):
    role:str
    active:bool=True
    feishu_open_id:str|None=Field(default=None,max_length=120)
class PasswordBody(BaseModel):
    current_password:str=Field(max_length=128)
    new_password:str=Field(max_length=128)
class ConnectBody(BaseModel):
    shop:str=Field(max_length=160)
class TaskBody(BaseModel):
    command:str=Field(min_length=1,max_length=2000)
    kind:str="chat"
    params:dict=Field(default_factory=dict)
    connection_id:str|None=None
    idempotency_key:str|None=Field(default=None,max_length=120)

WRITE_FIELDS = {"inventory": {"item_id","location_id","quantity"}, "product": {"product_id","description"},
    "fulfill": {"order_id","company","number"}, "tracking": {"order_id","fulfillment_id","company","number"},
    "cancel": {"order_id","reason"}, "refund": {"order_id","amount","currency"}, "pool_adjust": {"pool_id","quantity","safety"},
    "sandbox_sku": {"sku"}, "sandbox_stock": {"sku","quantity"}, "mcf_create": {"sku","quantity"}, "mcf_cancel": {"order_id"},
    "sandbox_status": {"order_id","status"}, "sandbox_package": {"order_id","package_id","status"}}
class DecisionBody(BaseModel):
    approved:bool
    note:str=Field(default="",max_length=500)
class CommandBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    command: str = Field(min_length=1, max_length=2000)
    connection_ids: list[str] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(min_length=1, max_length=120)
    use_ai: bool = True
class AmazonConnect(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    label: str = Field(default="Amazon 店铺", min_length=1, max_length=120)
    region: Literal["na", "eu", "fe"] = "na"
    environment: Literal["sandbox", "production"] = "sandbox"
    marketplace_id: str = Field(default="ATVPDKIKX0DER", min_length=5, max_length=30)
    client_id: str = Field(min_length=10, max_length=500)
    client_secret: str = Field(min_length=10, max_length=2048)
    refresh_token: str = Field(min_length=10, max_length=2048)
class PoolBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    name: str = Field(min_length=1, max_length=120)
    sku: str = Field(min_length=1, max_length=100)
    warehouse: str = Field(min_length=1, max_length=100)
    safety: int = Field(default=5, ge=0, le=1000000)
    pool_type: Literal["merchant", "platform"] = "merchant"
class MappingBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    connection_id: str
    variant_id: str = Field(min_length=1, max_length=254)
    location_id: str = Field(min_length=1, max_length=254)
class AllocationBody(BaseModel):
    model_config = {"extra": "forbid", "strict": True}
    targets: dict[str, int]
class ReconcileBody(BaseModel):
    note:str=Field(min_length=1,max_length=500)
class PrivacyBody(ReconcileBody):
    erase_snapshots:bool=False
class KnowledgeBody(BaseModel):
    title:str=Field(min_length=1,max_length=200)
    market:str=Field(min_length=1,max_length=20)
    content:str=Field(min_length=1,max_length=10000)
    evidence:list[str]=Field(min_length=1,max_length=30)
class FeishuConnect(BaseModel):
    app_id:str=Field(min_length=3,max_length=100)
    app_secret:str=Field(min_length=10,max_length=200)
    delivery:Literal["https-webhook","websocket"]="https-webhook"
    verification_token:str=Field(default="",max_length=200)
    encrypt_key:str=Field(default="",max_length=200)
    tenant_key:str=Field(default="",max_length=100)
class SettingsBody(BaseModel):
    sync_enabled:bool=False
    sync_interval_minutes:int=Field(default=30,ge=10,le=1440)
class Ticket(BaseModel):
    id:str=Field(min_length=1,max_length=100)
    market:str=Field(min_length=1,max_length=20)
    text:str=Field(min_length=1,max_length=3000)
class TicketImport(BaseModel):
    records:list[Ticket]=Field(min_length=1,max_length=500)

def public_user(u): return {k:u[k] for k in ("id","tenant_id","email","name","role","active","feishu_open_id")}
def email(value):
    import re
    v=value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",v): raise BusinessError("邮箱格式无效")
    return v

def build_services(settings=None,client_factory=None):
    settings=settings or Settings.load();settings.directory.mkdir(parents=True,exist_ok=True);settings.validate()
    db=Database(settings.database_url);vault=Vault(settings);ops=Operations(db,vault,settings,client_factory);feishu=Feishu(db,vault,ops,settings)
    return db,vault,ops,feishu,settings

def create_app(settings=None,client_factory=None):
    db,vault,ops,feishu,settings=build_services(settings,client_factory)
    worker=Worker(db,ops,feishu)
    setup_path=settings.directory/"setup.token"
    if not setup_path.exists():
        try:
            fd=os.open(setup_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,"w") as f: f.write(os.getenv("SETUP_TOKEN") or secrets.token_urlsafe(32))
        except FileExistsError:pass
    setup_token=setup_path.read_text().strip()
    if not setup_token:raise RuntimeError("初始设置码文件为空，服务已停止；请修复私有配置后重启")
    @asynccontextmanager
    async def lifespan(app):
        if settings.worker_enabled: worker.start()
        yield
        worker.stop();db.engine.dispose()
    app=FastAPI(title="小跨运营官 · 多平台商户服务",version="0.4.0",lifespan=lifespan,docs_url=None,openapi_url=None)
    app.state.db,app.state.vault,app.state.ops,app.state.feishu,app.state.worker,app.state.settings=db,vault,ops,feishu,worker,settings
    hosts=[urlparse(settings.public_url).hostname,"127.0.0.1","localhost","testserver"]
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=list(set(hosts)))
    public_paths={"/api/health","/api/ready","/api/bootstrap","/api/auth/login","/api/auth/register","/api/auth/invite/accept","/api/shopify/callback","/api/feishu/callback"}

    @app.middleware("http")
    async def security(request,call_next):
        path=request.url.path
        if request.method not in ("GET","HEAD"):
            try:length=int(request.headers.get("content-length","0") or 0)
            except ValueError:return JSONResponse({"detail":"请求长度无效"},400)
            if length>262144: return JSONResponse({"detail":"请求体过大"},413)
            content=await request.body()
            if len(content)>262144: return JSONResponse({"detail":"请求体过大"},413)
        identity=None;session=None
        raw=request.cookies.get("xiaokua_session","")
        if raw:
            with db.engine.connect() as conn:
                session=row(conn.execute(select(sessions).where(sessions.c.digest==digest(raw),sessions.c.expires_at>clock())).first())
                if session: identity=db.get(users,session["user_id"],session["tenant_id"],conn)
            if identity and not identity["active"]: identity=None
        request.state.identity,request.state.session=identity,session
        if path.startswith("/api/") and path not in public_paths:
            if not identity: return JSONResponse({"detail":"请登录工作台"},401)
        if path.startswith("/api/") and request.method not in ("GET","HEAD","OPTIONS"):
            origin=request.headers.get("origin","")
            allowed={settings.public_url}
            if not settings.production: allowed.update(("http://127.0.0.1:8000","http://localhost:8000","http://127.0.0.1:5173","http://localhost:5173"))
            if origin and origin not in allowed: return JSONResponse({"detail":"跨站请求已拒绝"},403)
            expected=session["csrf"] if identity else request.cookies.get("xiaokua_guest","")
            if not expected or not secrets.compare_digest(expected,request.headers.get("x-csrf-token","")): return JSONResponse({"detail":"会话校验失败，请刷新页面"},403)
        response=await call_next(request)
        response.headers.update({"X-Content-Type-Options":"nosniff","Referrer-Policy":"same-origin","X-Frame-Options":"DENY",
            "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; object-src 'none'"})
        if settings.production: response.headers["Strict-Transport-Security"]="max-age=31536000"
        if path.startswith("/api/") or path.startswith("/hooks/"): response.headers["Cache-Control"]="no-store"
        return response

    @app.exception_handler(BusinessError)
    @app.exception_handler(ValueError)
    async def invalid(request,error): return JSONResponse({"detail":str(error)},422)
    @app.exception_handler(IntegrityError)
    async def conflict(request,error): return JSONResponse({"detail":"记录已存在或发生并发冲突，请刷新后重试"},409)
    @app.exception_handler(ShopifyError)
    @app.exception_handler(PlatformError)
    @app.exception_handler(FeishuError)
    async def platform_error(request,error): return JSONResponse({"detail":str(error)},400)

    def actor(request,min_role="viewer"):
        u=request.state.identity
        if not u: raise HTTPException(401,"请登录")
        if ROLES[u["role"]]<ROLES[min_role]: raise HTTPException(403,"当前角色没有此操作权限")
        return u
    def issue_session(u,response):
        raw,csrf=secrets.token_urlsafe(48),secrets.token_urlsafe(32)
        with db.transaction() as conn:
            conn.execute(sessions.insert().values(id=uid("session"),tenant_id=u["tenant_id"],user_id=u["id"],digest=digest(raw),csrf=csrf,expires_at=clock()+43200,created_at=clock()))
        response.set_cookie("xiaokua_session",raw,httponly=True,secure=settings.production,samesite="lax",max_age=43200,path="/")
        response.delete_cookie("xiaokua_guest")
        return {"user":public_user(u),"csrf":csrf}

    @app.get("/api/health")
    def health(): return {"ok":True,"mode":"commerce-agent","version":"0.4.0"}
    @app.get("/api/ready")
    def ready():
        try:
            with db.engine.connect() as c:
                c.execute(select(func.count()).select_from(tenants))
                if settings.production and not c.execute(select(runtime_workers.c.id).where(runtime_workers.c.heartbeat>clock()-90)).first():return JSONResponse({"ok":False,"reason":"worker unavailable"},503)
            return {"ok":True}
        except Exception: return JSONResponse({"ok":False},503)
    @app.get("/api/bootstrap")
    def bootstrap(request:Request,response:Response):
        with db.engine.connect() as c: first=c.execute(select(func.count()).select_from(tenants)).scalar()==0
        if request.state.identity: return {"user":public_user(request.state.identity),"csrf":request.state.session["csrf"],"test_only":settings.test_only,"demo":getattr(app.state,"demo",False),"setup_required":False}
        guest=secrets.token_urlsafe(32)
        response.set_cookie("xiaokua_guest",guest,httponly=True,secure=settings.production,samesite="strict",max_age=3600,path="/")
        local=request.client and request.client.host in ("127.0.0.1","::1")
        return {"user":None,"csrf":guest,"setup_required":first,"setup_token":setup_token if first and local and not settings.production else None,
            "registration_enabled":settings.allow_registration,"test_only":settings.test_only,"demo":getattr(app.state,"demo",False)}

    @app.post("/api/auth/register",status_code=201)
    def register(body:Signup,request:Request,response:Response):
        if db.limited(digest("register:"+request.client.host),5,3600): raise HTTPException(429,"创建次数过多，请稍后重试")
        with db.transaction() as conn:
            count=conn.execute(select(func.count()).select_from(tenants)).scalar()
            if count==0:
                if not secrets.compare_digest(body.setup_token,setup_token): raise HTTPException(403,"初始设置码无效")
            elif not settings.allow_registration: raise HTTPException(403,"暂未开放公开注册，请由组织负责人邀请")
            encoded=password_hash(body.password)
            t=uid("org");u=dict(id=uid("user"),tenant_id=t,email=email(body.email),name=body.name,password=encoded,role="owner",active=True,feishu_open_id=None,created_at=clock())
            conn.execute(tenants.insert().values(id=t,name=body.organization,audit_head="0"*64,settings={"sync_enabled":False,"sync_interval_minutes":30},created_at=clock()))
            conn.execute(users.insert().values(**u));db.audit(conn,t,u["id"],"organization.created",t,{"name":body.organization})
        return issue_session(u,response)

    @app.post("/api/auth/login")
    def login(body:AuthBody,request:Request,response:Response):
        key=digest("login:"+request.client.host+":"+body.email.lower())
        if db.limited(key,6,300): raise HTTPException(429,"登录尝试过多，请 5 分钟后重试")
        with db.engine.connect() as c: u=row(c.execute(select(users).where(users.c.email==email(body.email),users.c.active==True)).first())
        if not u or not password_ok(u["password"],body.password): raise HTTPException(401,"邮箱或密码错误")
        with db.transaction() as c: db.audit(c,u["tenant_id"],u["id"],"session.login",u["id"],{})
        return issue_session(u,response)

    @app.post("/api/auth/logout")
    def logout(request:Request,response:Response):
        u=actor(request)
        with db.transaction() as c:
            c.execute(sessions.delete().where(sessions.c.id==request.state.session["id"],sessions.c.tenant_id==u["tenant_id"]))
        response.delete_cookie("xiaokua_session");return {"ok":True}

    @app.post("/api/auth/password")
    def change_password(body:PasswordBody,request:Request):
        u=actor(request)
        if not password_ok(u["password"],body.current_password): raise HTTPException(403,"当前密码错误")
        encoded=password_hash(body.new_password)
        with db.transaction() as c:
            c.execute(update(users).where(users.c.id==u["id"]).values(password=encoded))
            c.execute(sessions.delete().where(sessions.c.user_id==u["id"],sessions.c.id!=request.state.session["id"]))
            db.audit(c,u["tenant_id"],u["id"],"password.changed",u["id"],{})
        return {"ok":True}

    @app.post("/api/members/invite")
    def invite(body:InviteBody,request:Request):
        u=actor(request,"owner")
        if body.role not in ROLES: raise BusinessError("角色无效")
        token=secrets.token_urlsafe(32)
        with db.transaction() as c:
            c.execute(invitations.insert().values(id=uid("invite"),tenant_id=u["tenant_id"],email=email(body.email),role=body.role,digest=digest(token),expires_at=clock()+86400,used=False,created_at=clock()))
            db.audit(c,u["tenant_id"],u["id"],"member.invited",u["tenant_id"],{"role":body.role})
        return {"url":settings.public_url+"/?invite="+token,"expires_in":86400}

    @app.post("/api/auth/invite/accept",status_code=201)
    def accept_invite(body:AcceptInvite,response:Response):
        encoded=password_hash(body.password)
        with db.transaction() as c:
            invite=row(c.execute(select(invitations).where(invitations.c.digest==digest(body.token),invitations.c.used==False,invitations.c.expires_at>clock()).with_for_update()).first())
            if not invite or invite["email"]!=email(body.email): raise HTTPException(403,"邀请无效、已使用或邮箱不匹配")
            u=dict(id=uid("user"),tenant_id=invite["tenant_id"],email=invite["email"],name=body.name,password=encoded,role=invite["role"],active=True,feishu_open_id=None,created_at=clock())
            c.execute(users.insert().values(**u));c.execute(update(invitations).where(invitations.c.id==invite["id"]).values(used=True))
            db.audit(c,u["tenant_id"],u["id"],"member.joined",u["id"],{"role":u["role"]})
        return issue_session(u,response)

    @app.post("/api/members/{user_id}")
    def member(user_id:str,body:MemberBody,request:Request):
        u=actor(request,"owner")
        if body.role not in ROLES: raise BusinessError("角色无效")
        with db.transaction() as c:
            c.execute(select(tenants).where(tenants.c.id==u["tenant_id"]).with_for_update()).first()
            old=db.get(users,user_id,u["tenant_id"],c)
            if not old: raise HTTPException(404,"成员不存在")
            if old["role"]=="owner" and (body.role!="owner" or not body.active):
                owners=c.execute(select(func.count()).select_from(users).where(users.c.tenant_id==u["tenant_id"],users.c.role=="owner",users.c.active==True)).scalar()
                if owners<=1: raise BusinessError("组织必须保留至少一名负责人")
            c.execute(update(users).where(users.c.id==user_id,users.c.tenant_id==u["tenant_id"]).values(role=body.role,active=body.active,feishu_open_id=body.feishu_open_id or None))
            c.execute(sessions.delete().where(sessions.c.user_id==user_id))
            db.audit(c,u["tenant_id"],u["id"],"member.updated",user_id,{"role":body.role,"active":body.active,"feishu_bound":bool(body.feishu_open_id)})
        return {"ok":True}

    @app.get("/api/state")
    def state(request:Request):
        u=actor(request);t=u["tenant_id"]
        cs=db.list(connections,t)
        for con in cs:
            con.pop("secret")
            con["capabilities"] = connection_capabilities(con)
        with db.engine.connect() as c:
            rs=[row(r) for r in c.execute(select(resources).where(resources.c.tenant_id==t,resources.c.kind.in_(["order","variant"])).limit(10000))]
        connection_map = {con["id"]: con for con in cs}
        for resource in rs:
            con = connection_map.get(resource["connection_id"])
            if con and con["provider"] in PROVIDERS:
                resource["normalized"] = normalize_resource(con["provider"], resource["kind"], resource["data"], con["info"].get("demo", False))
        members=[public_user(x) for x in db.list(users,t)] if u["role"]=="owner" else []
        with db.engine.connect() as c:
            privacy=[row(r) for r in c.execute(select(events).join(connections,connections.c.id==events.c.connection_id).where(connections.c.tenant_id==t,events.c.topic.in_(["customers/data_request","customers/redact"])).order_by(events.c.created_at.desc()).limit(100))] if u["role"]=="owner" else []
        return {"organization":db.get(tenants,t),"user":public_user(u),"test_only":settings.test_only,"connections":cs,
            "orders":[r for r in rs if r["kind"]=="order"],"variants":[r for r in rs if r["kind"]=="variant"],
            "tasks":db.list(tasks,t,100),"actions":db.list(actions,t,100),"knowledge":db.list(knowledge,t,200),
            "members":members,"audit":db.list(audits,t,100),"audit_verification":db.verify(t),"jobs":db.list(jobs,t,60),
            "privacy_requests":privacy,"reserved":["Meta Ads","采购支付"],"platforms":[spec.public() for spec in PROVIDERS.values()],
            "shared_inventory":ops.shared.state(t),"deployment":{"database":db.engine.dialect.name,"https":settings.public_url.startswith("https://"),"worker_enabled":settings.worker_enabled}}

    @app.post("/api/commands", status_code=201)
    def command(body: CommandBody, request: Request):
        u = actor(request, "operator")
        if db.limited(digest("tasks:"+u["tenant_id"]), 60, 60):
            raise HTTPException(429, "任务提交过于频繁")
        return submit_command(ops, u, redact(body.command), body.connection_ids, "web", body.idempotency_key,
            use_ai=body.use_ai and not getattr(app.state, "demo", False))

    @app.post("/api/stock/pools", status_code=201)
    def pool_create(body: PoolBody, request: Request):
        return ops.shared.create_pool(actor(request, "owner"), **body.model_dump())

    @app.post("/api/stock/pools/{pool_id}/mappings", status_code=201)
    def mapping_create(pool_id: str, body: MappingBody, request: Request):
        return ops.shared.add_mapping(actor(request, "owner"), pool_id, **body.model_dump())

    @app.post("/api/stock/pools/{pool_id}/allocate", status_code=201)
    def stock_allocate(pool_id: str, body: AllocationBody, request: Request):
        return ops.shared.create_plan(actor(request, "operator"), pool_id, body.targets)

    @app.post("/api/stock/plans/{plan_id}/close")
    def close_allocation(plan_id: str, body: ReconcileBody, request: Request):
        return ops.shared.close_plan(actor(request, "owner"), plan_id, body.note)

    @app.post("/api/stock/pools/{pool_id}/clear-freeze")
    def stock_clear(pool_id: str, body: ReconcileBody, request: Request):
        return ops.shared.clear_freeze(actor(request, "owner"), pool_id, body.note)

    @app.post("/api/tasks",status_code=201)
    def submit(body:TaskBody,request:Request):
        u=actor(request,"operator")
        if db.limited(digest("tasks:"+u["tenant_id"]),60,60): raise HTTPException(429,"任务提交过于频繁")
        if any(key.startswith("_") or key in ("depends_on", "parent_command", "entrypoint") for key in body.params):
            raise BusinessError("任务内部字段只能由服务端生成")
        if body.kind in WRITE_FIELDS and set(body.params) - WRITE_FIELDS[body.kind]:
            raise BusinessError("操作包含未开放的业务参数，请使用工作台表单")
        return ops.submit(u,redact(body.command),body.kind,body.params,body.connection_id,body.idempotency_key)
    @app.get("/api/tasks/{task_id}")
    def get_task(task_id:str,request:Request):
        u=actor(request);task=db.get(tasks,task_id,u["tenant_id"])
        if not task: raise HTTPException(404,"任务不存在")
        return dict(task,actions=db.list(actions,u["tenant_id"],20,task_id=task_id))
    @app.post("/api/tasks/{task_id}/retry")
    def retry(task_id:str,request:Request):
        u=actor(request,"operator")
        with db.transaction() as c:
            task=db.get(tasks,task_id,u["tenant_id"],c)
            if not task or task["status"] not in ("failed","partial"): raise BusinessError("仅失败或部分完成的只读任务可以重试")
            if task["kind"] not in ("sync","analyze","chat","learn"): raise BusinessError("业务写入须重新生成方案，不能直接重试")
            c.execute(update(tasks).where(tasks.c.id==task_id).values(status="queued",result={}))
            c.execute(update(jobs).where(jobs.c.kind=="task",jobs.c.target_id==task_id,jobs.c.tenant_id==u["tenant_id"]).values(status="queued",available_at=clock(),error=""))
            db.audit(c,u["tenant_id"],u["id"],"task.retried",task_id,{})
        return {"ok":True}

    @app.post("/api/approvals/{action_id}")
    def decide(action_id:str,body:DecisionBody,request:Request):
        u=actor(request,"approver");return ops.decide(u,action_id,body.approved,body.note)

    @app.get("/api/actions/{action_id}/verify")
    def verify_action(action_id:str,request:Request):
        u=actor(request,"approver");a=db.get(actions,action_id,u["tenant_id"])
        if not a: raise HTTPException(404,"操作不存在")
        expected = a["before"].get("quantity", 0) + a["payload"].get("quantity", 0) if a["kind"] == "sandbox_stock" else None
        p = dict(a["payload"], _action_id=action_id, _expected_quantity=a["result"].get("expected_quantity", expected))
        if a["kind"] == "pool_adjust":
            observed = db.get(inventory_pools, p["pool_id"], u["tenant_id"])
        else:
            matched, proof = ops.client(a["connection_id"],u["tenant_id"]).verify_action(a["kind"], p)
            observed = {"matches_target": matched, **proof}
        return {"status":a["status"],"observed":observed,"message":"只读核实结果；待核实操作不会再次自动发送。请与平台日志核对。"}
    @app.post("/api/actions/{action_id}/reconcile")
    def reconcile(action_id:str,body:ReconcileBody,request:Request):
        return ops.reconcile(actor(request,"approver"),action_id,body.note)
    @app.get("/api/tasks/{task_id}/report")
    def task_report(task_id:str,request:Request):
        task=get_task(task_id,request)
        text=f"# 小跨运营报告\n\n任务：{task['command']}\n\n状态：{task['status']}\n\n{task['result'].get('summary','')}\n"
        for r in task['result'].get('reports',[]):text+=f"\n## {r['role']}\n\n{r['finding']}\n\n证据：{', '.join(r['evidence'])}\n\n模式：{r['mode']}\n"
        if task['result'].get('analysis'):
            text+='\n## 执行与上下文\n\n```json\n'+json.dumps({k:task['result'][k] for k in ('analysis','retrieval') if k in task['result']},ensure_ascii=False,indent=2)+'\n```\n'
        for child in task["result"].get("children", []):
            detail = db.get(tasks, child["id"], task["tenant_id"])
            if detail:
                text += f"\n## 店铺子任务 {detail['id']}\n\n类型：{detail['kind']} · 状态：{detail['status']}\n\n{detail['result'].get('summary', '')}\n"
        for r in task['result'].get('reports',[]):
            if r.get('limitations'):text+='\n### '+r['role']+' · 证据缺口\n\n'+'\n'.join('- '+v for v in r['limitations'])+'\n'
            if r.get('fallback_reason'):text+='\n回退原因：'+r['fallback_reason']+'\n'
        return PlainTextResponse(text,headers={"Content-Disposition":f'attachment; filename="{task_id}.md"'})

    @app.post("/api/knowledge",status_code=201)
    def add_knowledge(body:KnowledgeBody,request:Request):
        u=actor(request,"operator");entry=dict(id=uid("skill"),tenant_id=u["tenant_id"],title=body.title,market=body.market,content=redact(body.content),evidence=[redact(x)[:2000] for x in body.evidence],active=True,created_at=clock())
        with db.transaction() as c:
            c.execute(knowledge.insert().values(**entry));db.audit(c,u["tenant_id"],u["id"],"knowledge.saved",entry["id"],{"title":body.title,"market":body.market,"evidence_count":len(body.evidence)})
        return entry
    @app.post("/api/support/import")
    def import_support(body:TicketImport,request:Request):
        from .shopify import upsert_resource
        u=actor(request,"operator")
        with db.transaction() as c:
            for record in body.records:
                upsert_resource(c,u["tenant_id"],"support-"+u["tenant_id"],"ticket",{"id":record.id,"market":record.market.upper(),"text":redact(record.text)})
            db.audit(c,u["tenant_id"],u["id"],"support.imported",u["tenant_id"],{"count":len(body.records)})
        return {"ok":True,"count":len(body.records)}
    @app.get("/api/support/template")
    def support_template(request:Request):
        actor(request,"operator")
        return Response('\ufeffid,market,text\r\nticket-example-001,DE,请填写已去除个人信息的客服咨询\r\n',media_type="text/csv",headers={"Content-Disposition":'attachment; filename="xiaokua-support-template.csv"'})
    @app.post("/api/knowledge/{skill_id}/toggle")
    def toggle_skill(skill_id:str,body:dict,request:Request):
        u=actor(request,"operator")
        if type(body.get("active")) is not bool: raise BusinessError("active 必须为布尔值")
        with db.transaction() as c:
            if not db.get(knowledge,skill_id,u["tenant_id"],c): raise HTTPException(404,"技能不存在")
            c.execute(update(knowledge).where(knowledge.c.id==skill_id,knowledge.c.tenant_id==u["tenant_id"]).values(active=body["active"]))
            db.audit(c,u["tenant_id"],u["id"],"knowledge.toggled",skill_id,{"active":body["active"]})
        return {"ok":True}
    @app.get("/api/knowledge/{skill_id}/download")
    def download_skill(skill_id:str,request:Request):
        u=actor(request);k=db.get(knowledge,skill_id,u["tenant_id"])
        if not k: raise HTTPException(404,"技能不存在")
        from .skills import markdown,skill_name
        return PlainTextResponse(markdown(k),headers={"Content-Disposition":f'attachment; filename="{skill_name(k)}-SKILL.md"'})

    @app.get("/api/knowledge/{skill_id}/bundle")
    def download_skill_bundle(skill_id:str,request:Request):
        u=actor(request);k=db.get(knowledge,skill_id,u["tenant_id"])
        if not k: raise HTTPException(404,"技能不存在")
        from .skills import bundle
        name,content=bundle(k)
        return Response(content,media_type="application/zip",headers={"Content-Disposition":f'attachment; filename="{name}.zip"'})

    @app.post("/api/settings")
    def update_settings(body:SettingsBody,request:Request):
        u=actor(request,"owner")
        with db.transaction() as c:
            old=db.get(tenants,u["tenant_id"],db=c)
            c.execute(update(tenants).where(tenants.c.id==u["tenant_id"]).values(settings=dict(old["settings"],**body.model_dump())))
            db.audit(c,u["tenant_id"],u["id"],"settings.updated",u["tenant_id"],body.model_dump())
        return {"ok":True}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_notification(job_id:str,request:Request):
        u=actor(request,"owner")
        with db.transaction() as c:
            job=row(c.execute(select(jobs).where(jobs.c.id==job_id,jobs.c.tenant_id==u["tenant_id"]).with_for_update()).first())
            if not job or job["status"]!="failed" or job["kind"] not in ("notify","notify_result","notify_task"):raise BusinessError("仅失败的通知作业可在此重试，业务写入不能重复发送")
            c.execute(update(jobs).where(jobs.c.id==job_id).values(status="queued",available_at=clock(),error=""))
            db.audit(c,u["tenant_id"],u["id"],"notification.retried",job_id,{"kind":job["kind"]})
        return {"ok":True}

    def save_shop(u,domain,secret):
        api=Shopify(domain,secret["access_token"]);cap=api.capability()
        info={"shop":cap["shop"],"scopes":[s["handle"] for s in cap["currentAppInstallation"]["accessScopes"]]}
        with db.transaction() as c:
            old=row(c.execute(select(connections).where(connections.c.provider=="shopify",connections.c.external_id==domain).with_for_update()).first())
            if old and old["tenant_id"]!=u["tenant_id"]: raise BusinessError("该店铺已属于其他组织，需由原组织解除后重新授权")
            cid=old["id"] if old else uid("conn")
            values=dict(tenant_id=u["tenant_id"],provider="shopify",external_id=domain,secret=vault.seal(secret),status="active",info=info)
            if old: c.execute(update(connections).where(connections.c.id==cid).values(**values))
            else: c.execute(connections.insert().values(id=cid,**values,created_at=clock()))
            db.audit(c,u["tenant_id"],u["id"],"shopify.connected",cid,{"domain":domain,"scopes":info["scopes"]})
        return {"id":cid,"shop":info["shop"],"scopes":info["scopes"]}

    @app.post("/api/shopify/import-local")
    def import_local(request:Request):
        u=actor(request,"owner")
        if settings.production: raise HTTPException(403,"生产环境请使用商户 OAuth 授权")
        keys=credentials();domain=shop_domain(keys.get("SHOPIFY_SHOP_DOMAIN",""))
        values={"grant_type":"client_credentials","client_id":keys.get("SHOPIFY_CLIENT_ID",""),"client_secret":keys.get("SHOPIFY_CLIENT_SECRET","")}
        token=exchange(domain,values)
        return save_shop(u,domain,dict(values,**token,expires_at=clock()+token.get("expires_in",86399)))

    @app.post("/api/shopify/connect")
    def connect_shop(body:ConnectBody,request:Request):
        u=actor(request,"owner");domain=shop_domain(body.shop);keys=credentials()
        if not keys.get("SHOPIFY_CLIENT_ID") or not keys.get("SHOPIFY_CLIENT_SECRET"): raise BusinessError("服务尚未配置 Shopify 公共应用凭据")
        nonce=secrets.token_urlsafe(32)
        with db.transaction() as c:
            c.execute(oauth_states.insert().values(id=uid("oauth"),tenant_id=u["tenant_id"],user_id=u["id"],provider="shopify",external_id=domain,digest=digest(nonce),expires_at=clock()+600,used=False,created_at=clock()))
        query=urlencode({"client_id":keys["SHOPIFY_CLIENT_ID"],"scope":SCOPES,"redirect_uri":settings.public_url+"/api/shopify/callback","state":nonce})
        return {"url":f"https://{domain}/admin/oauth/authorize?{query}"}

    def save_amazon(u, body):
        if getattr(app.state, "demo", False):
            raise BusinessError("本机演示禁止真实平台凭据导入")
        from .platforms.amazon import AmazonAdapter
        secret = {"client_id": body.client_id, "client_secret": body.client_secret, "refresh_token": body.refresh_token}
        info = {"label": body.label, "region": body.region, "marketplace_id": body.marketplace_id, "environment": body.environment}
        api = AmazonAdapter(secret, info)
        api.token()
        api.inventory_summaries()
        info["capabilities"] = api.capabilities()
        external = body.environment+":"+body.region+":"+body.marketplace_id+":"+digest(body.refresh_token)[:28]
        with db.transaction() as c:
            existing = row(c.execute(select(connections).where(connections.c.provider == "amazon", connections.c.external_id == external).with_for_update()).first())
            if existing and existing["tenant_id"] != u["tenant_id"]:
                raise BusinessError("该店铺凭据已由另一个组织绑定")
            values = {"secret": vault.seal(secret), "status": "active", "info": info}
            if existing:
                identifier = existing["id"]
                c.execute(update(connections).where(connections.c.id == identifier).values(**values))
            else:
                identifier = uid("conn")
                c.execute(connections.insert().values(id=identifier, tenant_id=u["tenant_id"], provider="amazon", external_id=external, created_at=clock(), **values))
            db.audit(c, u["tenant_id"], u["id"], "amazon.connected", identifier, {"region": body.region, "marketplace_id": body.marketplace_id, "environment": body.environment})
        return {"id": identifier, "provider": "amazon", "capabilities": api.capabilities()}

    @app.post("/api/amazon/connect", status_code=201)
    def connect_amazon(body: AmazonConnect, request: Request):
        return save_amazon(actor(request, "owner"), body)

    @app.post("/api/amazon/import-local", status_code=201)
    def import_amazon(request: Request):
        u = actor(request, "owner")
        if settings.production:
            raise BusinessError("生产环境请显式录入商户自己的授权凭据")
        keys = credentials()
        return save_amazon(u, AmazonConnect(client_id=keys.get("AMAZON_LWA_CLIENT_ID", ""), client_secret=keys.get("AMAZON_LWA_CLIENT_SECRET", ""),
            refresh_token=keys.get("AMAZON_REFRESH_TOKEN", ""), region=os.getenv("AMAZON_REGION", "na"), marketplace_id=os.getenv("AMAZON_MARKETPLACE_ID", "ATVPDKIKX0DER")))

    @app.get("/api/shopify/callback")
    def callback(request:Request):
        if len(request.query_params.multi_items())!=len(set(request.query_params.keys())): raise HTTPException(403,"重复授权参数")
        p=dict(request.query_params);keys=credentials()
        if not shopify_query_hmac(p,keys.get("SHOPIFY_CLIENT_SECRET","")): raise HTTPException(403,"授权签名无效")
        try:
            if abs(clock()-float(p.get("timestamp","0")))>600: raise ValueError()
        except ValueError: raise HTTPException(403,"授权已过期") from None
        domain=shop_domain(p.get("shop",""));u=actor(request,"owner")
        with db.transaction() as c:
            st=row(c.execute(select(oauth_states).where(oauth_states.c.digest==digest(p.get("state","")),oauth_states.c.provider=="shopify",oauth_states.c.used==False,oauth_states.c.expires_at>clock()).with_for_update()).first())
            if not st or st["user_id"]!=u["id"] or st["tenant_id"]!=u["tenant_id"] or st["external_id"]!=domain: raise HTTPException(403,"授权状态无效")
            c.execute(update(oauth_states).where(oauth_states.c.id==st["id"]).values(used=True))
        values={"client_id":keys["SHOPIFY_CLIENT_ID"],"client_secret":keys["SHOPIFY_CLIENT_SECRET"],"code":p.get("code",""),"expiring":"1"}
        token=exchange(domain,values);save_shop(u,domain,dict(client_id=keys["SHOPIFY_CLIENT_ID"],client_secret=keys["SHOPIFY_CLIENT_SECRET"],**token,expires_at=clock()+token.get("expires_in",86399) if token.get("expires_in") else 0))
        return RedirectResponse(settings.public_url+"/?connected=shopify",303)

    @app.post("/api/connections/{connection_id}/disconnect")
    def disconnect(connection_id:str,request:Request):
        u=actor(request,"owner")
        with db.transaction() as c:
            con=db.get(connections,connection_id,u["tenant_id"],c)
            if not con: raise HTTPException(404,"连接不存在")
            c.execute(update(connections).where(connections.c.id==connection_id).values(status="revoked",secret=""))
            c.execute(resources.delete().where(resources.c.connection_id==connection_id,resources.c.tenant_id==u["tenant_id"]))
            c.execute(update(actions).where(actions.c.connection_id==connection_id,actions.c.status.in_(["pending","approved"])).values(status="rejected",reason="连接已解除"))
            db.audit(c,u["tenant_id"],u["id"],"connection.revoked",connection_id,{})
            c.execute(update(tasks).where(tasks.c.connection_id == connection_id, tasks.c.tenant_id == u["tenant_id"],
                tasks.c.status.in_(["queued", "running", "awaiting_approval"])).values(status="failed", result={"summary": "连接已解除，待执行任务已停止"}))
        ops.shared.disconnect(u["tenant_id"], connection_id)
        ops.refresh_commands(u["tenant_id"])
        return {"ok":True}

    @app.post("/hooks/shopify")
    async def shopify_hook(request:Request):
        body=await request.body();domain=shop_domain(request.headers.get("x-shopify-shop-domain",""))
        with db.engine.connect() as c: con=row(c.execute(select(connections).where(connections.c.provider=="shopify",connections.c.external_id==domain)).first())
        secret=vault.open(con["secret"])["client_secret"] if con and con["secret"] else credentials().get("SHOPIFY_CLIENT_SECRET","")
        if not secret or not body_hmac(body,request.headers.get("x-shopify-hmac-sha256",""),secret): raise HTTPException(401,"回调签名无效")
        topic=request.headers.get("x-shopify-topic","");eid=request.headers.get("x-shopify-event-id") or request.headers.get("x-shopify-webhook-id")
        if not eid or len(eid)>200: raise HTTPException(400,"缺少事件标识")
        if not con: return {"ok":True}
        if topic not in ("orders/create","orders/updated","orders/cancelled","refunds/create","fulfillments/create","fulfillments/update","inventory_levels/update","products/update","products/delete","app/uninstalled","customers/data_request","customers/redact","shop/redact"): return {"ok":True}
        with db.transaction() as c:
            old=c.execute(select(events.c.id).where(events.c.provider=="shopify",events.c.connection_id==con["id"],events.c.external_id==eid)).first()
            if old: return {"ok":True,"duplicate":True}
            # Do not persist raw payload: order webhooks include customer name/address/email.
            ev=uid("event")
            minimal={}
            if topic in ("customers/data_request","customers/redact"):
                import re
                value=json.loads(body);ids=value.get("orders_requested" if topic=="customers/data_request" else "orders_to_redact",[])
                if not isinstance(ids,list) or len(ids)>2000:raise HTTPException(400,"隐私请求订单列表无效")
                minimal={"order_ids":["gid://shopify/Order/"+str(x) for x in ids if re.fullmatch(r"\d{1,24}",str(x))]}
            c.execute(events.insert().values(id=ev,provider="shopify",connection_id=con["id"],external_id=eid,topic=topic,payload=minimal,status="queued",created_at=clock()))
            db.enqueue(c,con["tenant_id"],"shopify_event",ev)
            db.audit(c,con["tenant_id"],"shopify","webhook.received",ev,{"topic":topic})
        return {"ok":True}

    def privacy_case(event_id,u,c):
        ev=db.get(events,event_id,db=c)
        con=db.get(connections,ev["connection_id"],u["tenant_id"],c) if ev else None
        if not ev or not con or ev["topic"] not in ("customers/data_request","customers/redact"):raise HTTPException(404,"隐私请求不存在")
        return ev

    @app.get("/api/privacy/{event_id}/export")
    def privacy_export(event_id:str,request:Request):
        u=actor(request,"owner")
        with db.engine.connect() as c:
            ev=privacy_case(event_id,u,c);ids=ev["payload"].get("order_ids",[])
            snapshots=[row(r)["data"] for r in c.execute(select(resources).where(resources.c.tenant_id==u["tenant_id"],resources.c.connection_id==ev["connection_id"],resources.c.kind=="order",resources.c.external_id.in_(ids)))]
            related=[a for a in db.list(actions,u["tenant_id"],100000,connection_id=ev["connection_id"]) if a["payload"].get("order_id") in ids]
        return JSONResponse({"request_id":ev["external_id"],"orders":snapshots,"operations":[{k:a[k] for k in ("id","kind","status","payload","before","result")} for a in related],"note":"不采集客户姓名、邮件、电话、地址。财务审计记录的保留与披露由组织按适用义务处理。"},headers={"Content-Disposition":'attachment; filename="privacy-data.json"'})

    @app.post("/api/privacy/{event_id}/resolve")
    def privacy_resolve(event_id:str,body:PrivacyBody,request:Request):
        u=actor(request,"owner")
        with db.transaction() as c:
            ev=privacy_case(event_id,u,c)
            c.execute(select(connections.c.id).where(connections.c.id==ev["connection_id"]).with_for_update()).first()
            if ev["status"]!="needs_review":raise BusinessError("请求尚未进入人工处理或已记录处理结果")
            ids=ev["payload"].get("order_ids",[])
            if ev["topic"]=="customers/redact" and not body.erase_snapshots:raise BusinessError("删除请求需要清除对应订单快照")
            if body.erase_snapshots:
                from .shopify import upsert_resource
                for oid in ids:upsert_resource(c,u["tenant_id"],ev["connection_id"],"blocked_order",{"id":digest(oid),"reason":"privacy_redaction"})
                c.execute(resources.delete().where(resources.c.tenant_id==u["tenant_id"],resources.c.connection_id==ev["connection_id"],resources.c.kind=="order",resources.c.external_id.in_(ids)))
                for a in db.list(actions,u["tenant_id"],100000,connection_id=ev["connection_id"]):
                    if a["payload"].get("order_id") not in ids:continue
                    if a["status"] in ("executing","uncertain"):raise BusinessError("相关操作仍在执行或待核实，先核对财务结果后处理删除")
                    c.execute(update(actions).where(actions.c.id==a["id"]).values(payload={},before={},result={},status="rejected" if a["status"] in ("pending","approved") else a["status"],reason="订单隐私请求已处理"))
                    c.execute(update(tasks).where(tasks.c.id==a["task_id"],tasks.c.tenant_id==u["tenant_id"]).values(params={},result={"summary":"对应订单内容已按隐私请求清除"},status="redacted"))
            c.execute(update(events).where(events.c.id==event_id).values(status="reviewed",payload={"order_count":len(ids)}))
            db.audit(c,u["tenant_id"],u["id"],"privacy.reviewed",event_id,{"topic":ev["topic"],"snapshots_erased":body.erase_snapshots,"note":redact(body.note),"financial_audit_retained":True})
        return {"ok":True,"status":"reviewed"}

    @app.post("/api/feishu/connect")
    def feishu_connect(body:FeishuConnect,request:Request):
        u=actor(request,"owner")
        if body.delivery=="https-webhook" and (len(body.verification_token)<10 or len(body.encrypt_key)<10):
            raise BusinessError("HTTPS 回调必须配置有效的 Verification Token 和 Encrypt Key")
        secret=body.model_dump();token=tenant_token(secret)
        bot=feishu_request("GET","/bot/v3/info",token)
        with db.transaction() as c:
            external=body.app_id
            old=row(c.execute(select(connections).where(connections.c.provider=="feishu",connections.c.external_id==external)).first())
            if old and old["tenant_id"]!=u["tenant_id"]: raise BusinessError("此飞书自建应用已绑定其他组织")
            cid=old["id"] if old else uid("conn")
            values=dict(tenant_id=u["tenant_id"],provider="feishu",external_id=external,secret=vault.seal(secret),status="active",info={"bot_enabled":True,"tenant_key":body.tenant_key,"delivery":body.delivery})
            if old: c.execute(update(connections).where(connections.c.id==cid).values(**values))
            else: c.execute(connections.insert().values(id=cid,**values,created_at=clock()))
            db.audit(c,u["tenant_id"],u["id"],"feishu.connected",cid,{"bot_enabled":True})
        return {"id":cid,"delivery":body.delivery,"callback_url":settings.public_url+"/hooks/feishu/"+cid if body.delivery=="https-webhook" else None,"login_callback":settings.public_url+"/api/feishu/callback"}

    @app.post("/api/feishu/import-local")
    def feishu_import_local(request:Request):
        actor(request,"owner")
        if settings.production:raise HTTPException(403,"生产环境请配置组织自己的飞书应用")
        keys=credentials()
        if any(not keys.get(k) for k in ("FEISHU_APP_ID","FEISHU_APP_SECRET")):
            raise BusinessError("本机缺少飞书应用凭据")
        return feishu_connect(FeishuConnect(app_id=keys["FEISHU_APP_ID"],app_secret=keys["FEISHU_APP_SECRET"],delivery=os.getenv("FEISHU_DELIVERY","https-webhook"),verification_token=keys.get("FEISHU_VERIFICATION_TOKEN",""),encrypt_key=keys.get("FEISHU_ENCRYPT_KEY","")),request)

    @app.post("/hooks/feishu/{connection_id}")
    async def feishu_hook(connection_id:str,request:Request):
        con=db.get(connections,connection_id)
        if not con or con["provider"]!="feishu" or con["status"]!="active": raise HTTPException(404,"连接不存在")
        if con["info"].get("delivery","https-webhook")!="https-webhook": raise HTTPException(404,"此连接使用已认证长连接")
        try: value=verify_event(await request.body(),request.headers,vault.open(con["secret"]))
        except (FeishuError,ValueError): raise HTTPException(401,"飞书回调校验失败") from None
        if value.get("type")=="url_verification": return {"challenge":value.get("challenge","")}
        return feishu.receive(con,value)

    @app.post("/api/feishu/bind")
    def feishu_bind(request:Request):
        u=actor(request);cons=db.list(connections,u["tenant_id"],2,provider="feishu",status="active")
        if len(cons)!=1: raise BusinessError("请先配置一个有效的飞书连接")
        nonce=secrets.token_urlsafe(32);con=cons[0];secret=vault.open(con["secret"])
        with db.transaction() as c:
            c.execute(oauth_states.insert().values(id=uid("oauth"),tenant_id=u["tenant_id"],user_id=u["id"],provider="feishu",external_id=con["id"],digest=digest(nonce),expires_at=clock()+600,used=False,created_at=clock()))
        return {"url":"https://accounts.feishu.cn/open-apis/authen/v1/authorize?"+urlencode({"app_id":secret["app_id"],"redirect_uri":settings.public_url+"/api/feishu/callback","state":nonce})}

    @app.get("/api/feishu/callback")
    def feishu_callback(request:Request):
        u=actor(request);p=dict(request.query_params)
        if len(request.query_params.multi_items())!=len(set(request.query_params.keys())):raise HTTPException(403,"重复授权参数")
        with db.transaction() as c:
            st=row(c.execute(select(oauth_states).where(oauth_states.c.digest==digest(p.get("state","")),oauth_states.c.provider=="feishu",oauth_states.c.used==False,oauth_states.c.expires_at>clock()).with_for_update()).first())
            if not st or st["user_id"]!=u["id"] or st["tenant_id"]!=u["tenant_id"]: raise HTTPException(403,"飞书授权状态无效")
            con=db.get(connections,st["external_id"],u["tenant_id"],c)
            if not con or con["status"]!="active": raise HTTPException(403,"飞书连接已失效")
            c.execute(update(oauth_states).where(oauth_states.c.id==st["id"]).values(used=True))
        secret=vault.open(con["secret"])
        # Official authen v1 endpoint: its access token is consumed only to obtain a verified identity.
        app=feishu_request("POST","/auth/v3/app_access_token/internal",payload={"app_id":secret["app_id"],"app_secret":secret["app_secret"]})
        identity=feishu_request("POST","/authen/v1/oidc/access_token",app["app_access_token"],{"grant_type":"authorization_code","code":p.get("code","")})["data"]
        if con["info"].get("tenant_key") and identity.get("tenant_key")!=con["info"]["tenant_key"]: raise HTTPException(403,"飞书身份不属于该组织")
        with db.transaction() as c:
            c.execute(update(users).where(users.c.id==u["id"],users.c.tenant_id==u["tenant_id"]).values(feishu_open_id=identity["open_id"]))
            db.audit(c,u["tenant_id"],u["id"],"feishu.identity_bound",u["id"],{})
        return RedirectResponse(settings.public_url+"/?connected=feishu",303)

    @app.get("/api/audit/export")
    def audit_export(request:Request):
        u=actor(request,"approver");buffer=io.StringIO();writer=csv.writer(buffer)
        writer.writerow(["ID","Time","Actor","Event","Target","Detail","Previous Hash","Hash"])
        with db.engine.connect() as c:
            for r in c.execute(select(audits).where(audits.c.tenant_id==u["tenant_id"]).order_by(audits.c.created_at)):
                x=row(r)
                values=[x[k] for k in ("id","created_at","actor_id","event","target_id","detail","previous_hash","hash")]
                writer.writerow([("'"+str(v)) if str(v).startswith(("=","+","-","@","\t","\r")) else str(v) for v in values])
        return Response("\ufeff"+buffer.getvalue(),media_type="text/csv",headers={"Content-Disposition":'attachment; filename="xiaokua-audit.csv"'})

    dist=ROOT/"frontend"/"dist"
    if (dist/"assets").is_dir(): app.mount("/assets",StaticFiles(directory=dist/"assets"),name="assets")
    @app.get("/{path:path}")
    def frontend(path:str):
        if path.startswith(("api/","hooks/")): raise HTTPException(404,"接口不存在")
        if not (dist/"index.html").exists(): return PlainTextResponse("请先构建前端",503)
        return FileResponse(dist/"index.html")
    return app
