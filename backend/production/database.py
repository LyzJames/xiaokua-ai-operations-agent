import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import (Boolean, Column, Float, Integer, JSON, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, event, select, update)

meta = MetaData()
def table(name, *columns, **kw):
    return Table(name, meta, Column("id", String(64), primary_key=True), *columns, **kw)
def tenant(): return Column("tenant_id", String(64), nullable=False, index=True)
def stamp(): return Column("created_at", Float, nullable=False)

tenants = table("tenants", Column("name", String(120), nullable=False), Column("audit_head", String(64), nullable=False, default="0"*64),
    Column("settings", JSON, nullable=False), stamp())
users = table("users", tenant(), Column("email", String(254), nullable=False, unique=True), Column("name", String(100), nullable=False),
    Column("password", Text, nullable=False), Column("role", String(20), nullable=False), Column("active", Boolean, nullable=False, default=True),
    Column("feishu_open_id", String(120)), stamp(), UniqueConstraint("tenant_id", "feishu_open_id"))
sessions = table("sessions", tenant(), Column("user_id", String(64), nullable=False), Column("digest", String(64), nullable=False, unique=True),
    Column("csrf", String(80), nullable=False), Column("expires_at", Float, nullable=False), stamp())
invitations = table("invitations", tenant(), Column("email", String(254), nullable=False), Column("role", String(20), nullable=False),
    Column("digest", String(64), nullable=False, unique=True), Column("expires_at", Float, nullable=False), Column("used", Boolean, default=False, nullable=False), stamp())
connections = table("connections", tenant(), Column("provider", String(20), nullable=False), Column("external_id", String(254), nullable=False),
    Column("secret", Text, nullable=False), Column("status", String(30), nullable=False), Column("info", JSON, nullable=False), stamp(),
    UniqueConstraint("provider", "external_id"))
oauth_states = table("oauth_states", tenant(), Column("user_id", String(64), nullable=False), Column("provider", String(20), nullable=False),
    Column("external_id", String(254), nullable=False), Column("digest", String(64), unique=True, nullable=False),
    Column("expires_at", Float, nullable=False), Column("used", Boolean, default=False, nullable=False), stamp())
resources = table("resources", tenant(), Column("connection_id", String(64), nullable=False), Column("kind", String(30), nullable=False),
    Column("external_id", String(254), nullable=False), Column("data", JSON, nullable=False), Column("updated_at", Float, nullable=False),
    UniqueConstraint("connection_id", "kind", "external_id"))
tasks = table("tasks", tenant(), Column("actor_id", String(64), nullable=False), Column("connection_id", String(64)),
    Column("command", Text, nullable=False), Column("kind", String(30), nullable=False), Column("params", JSON, nullable=False),
    Column("status", String(30), nullable=False), Column("result", JSON, nullable=False), Column("idem", String(120), nullable=False), stamp(),
    UniqueConstraint("tenant_id", "idem"))
actions = table("actions", tenant(), Column("task_id", String(64), nullable=False), Column("connection_id", String(64), nullable=False),
    Column("actor_id", String(64), nullable=False), Column("kind", String(30), nullable=False), Column("title", String(200), nullable=False),
    Column("payload", JSON, nullable=False), Column("before", JSON, nullable=False), Column("status", String(30), nullable=False),
    Column("reason", Text, nullable=False), Column("approved_by", String(64)), Column("note", Text, nullable=False, default=""),
    Column("result", JSON, nullable=False), Column("expires_at", Float, nullable=False), stamp())
jobs = table("jobs", tenant(), Column("kind", String(30), nullable=False), Column("target_id", String(64), nullable=False),
    Column("status", String(30), nullable=False), Column("attempts", Integer, nullable=False, default=0),
    Column("available_at", Float, nullable=False), Column("lease_until", Float, nullable=False, default=0), Column("worker", String(64)),
    Column("error", Text, nullable=False, default=""), stamp(), UniqueConstraint("kind", "target_id"))
audits = table("audits", tenant(), Column("actor_id", String(100), nullable=False), Column("event", String(80), nullable=False),
    Column("target_id", String(254), nullable=False), Column("detail", JSON, nullable=False), Column("previous_hash", String(64), nullable=False),
    Column("hash", String(64), nullable=False), stamp())
events = table("events", Column("provider", String(20), nullable=False), Column("external_id", String(254), nullable=False),
    Column("connection_id", String(64), nullable=False), Column("topic", String(100), nullable=False), Column("payload", JSON, nullable=False),
    Column("status", String(30), nullable=False), stamp(), UniqueConstraint("provider", "connection_id", "external_id"))
knowledge = table("knowledge", tenant(), Column("title", String(200), nullable=False), Column("market", String(20), nullable=False),
    Column("content", Text, nullable=False), Column("evidence", JSON, nullable=False), Column("active", Boolean, default=True, nullable=False), stamp())
rate_limits = table("rate_limits", Column("count", Integer, nullable=False), Column("reset_at", Float, nullable=False))
runtime_workers = table("runtime_workers", Column("heartbeat", Float, nullable=False))

def uid(prefix="id"): return prefix + "_" + uuid.uuid4().hex
def clock(): return time.time()
def canonical(v): return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def row(value): return dict(value._mapping) if value is not None else None

class Database:
    def __init__(self, url: str):
        self.url = url
        self.engine = create_engine(url, pool_pre_ping=True, **({"connect_args":{"check_same_thread":False,"timeout":30}} if url.startswith("sqlite") else {"pool_size":8,"max_overflow":8}))
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def sqlite_options(conn, _):
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
        meta.create_all(self.engine)
        self.install_audit_guards()

    def install_audit_guards(self):
        with self.engine.begin() as db:
            if self.engine.dialect.name == "sqlite":
                for op in ("UPDATE", "DELETE"):
                    db.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS audit_no_{op.lower()} BEFORE {op} ON audits BEGIN SELECT RAISE(ABORT,'append-only audit'); END;")
            elif self.engine.dialect.name == "postgresql":
                db.exec_driver_sql("CREATE OR REPLACE FUNCTION xiaokua_audit_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append-only audit'; END; $$;")
                db.exec_driver_sql("DROP TRIGGER IF EXISTS audit_append_only ON audits")
                db.exec_driver_sql("CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audits FOR EACH ROW EXECUTE FUNCTION xiaokua_audit_guard()")

    @contextmanager
    def transaction(self):
        with self.engine.connect() as db:
            if self.engine.dialect.name == "sqlite": db.exec_driver_sql("BEGIN IMMEDIATE")
            else: db.begin()
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def get(self, tab, item_id, tenant_id=None, db=None):
        query = select(tab).where(tab.c.id == item_id)
        if tenant_id is not None: query = query.where(tab.c.tenant_id == tenant_id)
        if db is not None: return row(db.execute(query).first())
        with self.engine.connect() as conn: return row(conn.execute(query).first())

    def list(self, tab, tenant_id=None, limit=100, **filters):
        query = select(tab)
        if tenant_id is not None: query = query.where(tab.c.tenant_id == tenant_id)
        for k, v in filters.items(): query = query.where(tab.c[k] == v)
        if "created_at" in tab.c: query = query.order_by(tab.c.created_at.desc(), tab.c.id.desc())
        with self.engine.connect() as db: return [row(r) for r in db.execute(query.limit(limit))]

    def audit(self, db, tenant_id, actor, event_name, target, detail):
        # Lock the tenant head before appending so independent workers cannot fork the chain.
        t = row(db.execute(select(tenants).where(tenants.c.id == tenant_id).with_for_update()).first())
        if not t: raise ValueError("组织不存在")
        entry = {"id":uid("audit"),"tenant_id":tenant_id,"actor_id":actor,"event":event_name,"target_id":target,"detail":detail,"previous_hash":t["audit_head"],"created_at":clock()}
        digest = hashlib.sha256(canonical(entry).encode()).hexdigest()
        db.execute(audits.insert().values(**entry, hash=digest))
        db.execute(update(tenants).where(tenants.c.id == tenant_id).values(audit_head=digest))

    def verify(self, tenant_id):
        with self.engine.connect() as db:
            records = [row(r) for r in db.execute(select(audits).where(audits.c.tenant_id == tenant_id))]
            head = db.execute(select(tenants.c.audit_head).where(tenants.c.id == tenant_id)).scalar_one()
        by_previous = {}
        for r in records:
            h = r.pop("hash")
            if hashlib.sha256(canonical(r).encode()).hexdigest() != h or r["previous_hash"] in by_previous: return {"valid":False,"count":len(records)}
            by_previous[r["previous_hash"]] = h
        previous = "0"*64
        for _ in records:
            previous = by_previous.get(previous)
            if not previous: return {"valid":False,"count":len(records)}
        return {"valid":previous == head,"count":len(records),"head":head}

    def enqueue(self, db, tenant_id, kind, target_id):
        if db.execute(select(jobs.c.id).where(jobs.c.kind == kind, jobs.c.target_id == target_id)).first(): return
        db.execute(jobs.insert().values(id=uid("job"),tenant_id=tenant_id,kind=kind,target_id=target_id,status="queued",attempts=0,available_at=clock(),lease_until=0,worker=None,error="",created_at=clock()))

    def limited(self, key, maximum, seconds):
        with self.transaction() as db:
            current = row(db.execute(select(rate_limits).where(rate_limits.c.id == key).with_for_update()).first())
            if not current:
                db.execute(rate_limits.insert().values(id=key,count=1,reset_at=clock()+seconds)); return False
            if current["reset_at"] <= clock():
                db.execute(update(rate_limits).where(rate_limits.c.id == key).values(count=1, reset_at=clock()+seconds)); return False
            if current["count"] >= maximum: return True
            db.execute(update(rate_limits).where(rate_limits.c.id == key).values(count=current["count"]+1)); return False
