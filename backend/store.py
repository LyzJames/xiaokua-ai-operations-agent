import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")

def uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"

def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

class Store:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.path = directory / "operations.sqlite"
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, command TEXT NOT NULL, kind TEXT NOT NULL,
              status TEXT NOT NULL, use_ai INTEGER NOT NULL, params TEXT NOT NULL, plan TEXT NOT NULL DEFAULT '[]',
              result TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, idem TEXT UNIQUE);
            CREATE TABLE IF NOT EXISTS traces(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, node TEXT NOT NULL,
              message TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(task_id,node));
            CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
              title TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
              created_at TEXT NOT NULL, expires_at TEXT NOT NULL, decided_at TEXT, note TEXT, result TEXT NOT NULL DEFAULT '{}',
              UNIQUE(task_id,kind,title));
            CREATE TABLE IF NOT EXISTS inventory(sku TEXT PRIMARY KEY, name TEXT NOT NULL, stock INTEGER NOT NULL,
              reserved INTEGER NOT NULL, batch TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, country TEXT NOT NULL, channel TEXT NOT NULL,
              sku TEXT NOT NULL, quantity INTEGER NOT NULL, total_cents INTEGER NOT NULL, refunded_cents INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL, tracking TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL,
              sku TEXT NOT NULL, budget_cents INTEGER NOT NULL, status TEXT NOT NULL, roas REAL NOT NULL,
              spend_cents INTEGER NOT NULL, revenue_cents INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS tickets(id TEXT PRIMARY KEY, country TEXT NOT NULL, category TEXT NOT NULL,
              text TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS returns(id TEXT PRIMARY KEY, country TEXT NOT NULL, sku TEXT NOT NULL,
              batch TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, firmware TEXT NOT NULL, shipped INTEGER NOT NULL,
              qc_note TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, country TEXT NOT NULL, category TEXT NOT NULL,
              title TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL, confidence REAL NOT NULL,
              created_at TEXT NOT NULL, UNIQUE(country,category));
            CREATE TABLE IF NOT EXISTS skills(id TEXT PRIMARY KEY, memory_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
              content TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
              uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY, action_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
              amount_cents INTEGER NOT NULL, vendor TEXT, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
              actor TEXT NOT NULL, event TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL,
              previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'append-only audit'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'append-only audit'); END;
            CREATE TABLE IF NOT EXISTS bot_events(id TEXT PRIMARY KEY, task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS skill_uses(task_id TEXT NOT NULL, skill_id TEXT NOT NULL, PRIMARY KEY(task_id,skill_id));
            ''')
        self.seed()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def rows(self, sql, params=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.rows(sql, params)
        return rows[0] if rows else None

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value, db=None):
        if db is None:
            with self.transaction() as conn:
                self.set_setting(key, value, conn)
        else:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, dumps(value)))

    def audit(self, db, event, target, detail, actor="小跨"):
        previous = db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        timestamp = now()
        payload = {"created_at": timestamp, "actor": actor, "event": event,
                   "target": target, "detail": dumps(detail), "previous_hash": previous_hash}
        digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
        db.execute("INSERT INTO audit(created_at,actor,event,target,detail,previous_hash,hash) VALUES (?,?,?,?,?,?,?)",
                   (timestamp, actor, event, target, payload["detail"], previous_hash, digest))

    def verify_audit(self):
        previous = "0" * 64
        rows = self.rows("SELECT * FROM audit ORDER BY id")
        for row in rows:
            payload = {k: row[k] for k in ("created_at", "actor", "event", "target", "detail", "previous_hash")}
            digest = hashlib.sha256(dumps(payload).encode()).hexdigest()
            if row["previous_hash"] != previous or digest != row["hash"]:
                return {"valid": False, "count": len(rows), "broken_at": row["id"]}
            previous = row["hash"]
        return {"valid": True, "count": len(rows), "head": previous}

    def trace(self, task_id, node, message):
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO traces VALUES (?,?,?,?,?)", (uid("trace"), task_id, node, message, now()))

    def task(self, task_id):
        row = self.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            for key in ("params", "plan", "result"):
                row[key] = json.loads(row[key])
            row["traces"] = self.rows("SELECT * FROM traces WHERE task_id=? ORDER BY created_at,rowid", (task_id,))
            row["actions"] = self.actions(task_id)
        return row

    def actions(self, task_id=None):
        rows = self.rows("SELECT * FROM actions" + (" WHERE task_id=?" if task_id else "") + " ORDER BY created_at DESC,rowid DESC", (task_id,) if task_id else ())
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            row["result"] = json.loads(row["result"])
        return rows

    def seed(self):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM settings WHERE key='seeded'").fetchone():
                return
            defaults = {"seeded": True, "wallet_initial_cents": 250000, "scheduler_enabled": False,
                        "policy": {"refund_single_cents": 5000, "refund_daily_cents": 20000,
                                   "purchase_single_cents": 10000, "purchase_daily_cents": 30000,
                                   "vendor_whitelist": ["EcoPack EU", "PackRight UK"], "ads_approval": True}}
            for key, value in defaults.items():
                self.set_setting(key, value, db)
            products = [("LIGHT-A19", "Luma 智能灯泡 A19", 360, 0, "B-2409"),
                        ("PLUG-MINI", "Mini 智能插座", 620, 0, "P-2410"),
                        ("HUB-PRO", "Home Hub Pro", 85, 0, "H-2410")]
            db.executemany("INSERT INTO inventory VALUES (?,?,?,?,?)", products)
            stamp = datetime.now(TZ)
            for i in range(36):
                country = ["DE", "FR", "GB", "US", "DE", "FR"][i % 6]
                sku = products[i % 3][0]
                price = [2990, 2490, 8990][i % 3]
                status = "pending" if i < 12 else ("in_transit" if i < 30 else "delivered")
                db.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,0,?,?,?)", (f"EU-{1041+i}", country,
                    "Shopify" if i % 4 else "Amazon", sku, 1, price, status,
                    None if status == "pending" else f"SIM-DHL-{700000+i}", (stamp-timedelta(hours=i+1)).isoformat(timespec="seconds")))
            campaigns = [("AD-DE-01", "德国 · 节能生活", "DE", "PLUG-MINI", 6000, "active", 4.6, 41200, 189520),
                         ("AD-FR-01", "法国 · 智慧家庭", "FR", "HUB-PRO", 4500, "active", 3.8, 30200, 114760),
                         ("AD-GB-01", "英国 · 智能照明", "GB", "LIGHT-A19", 5500, "active", 1.7, 38100, 64770),
                         ("AD-US-01", "美国 · Connected Home", "US", "PLUG-MINI", 8000, "active", 4.1, 55600, 227960)]
            db.executemany("INSERT INTO campaigns VALUES (?,?,?,?,?,?,?,?,?)", campaigns)
            samples = [("DE", "energy", "产品的能源效率等级是什么？可以提供官方能效标签吗？"),
                       ("FR", "local_return", "是否支持法国本地退货？退货地址和运费怎么处理？"),
                       ("GB", "pairing", "灯泡无法完成 2.4 GHz Wi-Fi 配网，多次重置后仍断连。"),
                       ("US", "compatibility", "Does this support Matter and Alexa?")]
            for i in range(240):
                country, category, body = samples[i % 4]
                if i % 7 == 0:
                    category, body = "shipping", "请问订单何时送达？"
                db.execute("INSERT INTO tickets VALUES (?,?,?,?,?)", (f"CS-{i+1:04}", country, category, body,
                    (stamp-timedelta(days=i % 30)).isoformat(timespec="seconds")))
            for i in range(28):
                db.execute("INSERT INTO returns VALUES (?,?,?,?,?,?)", (f"RT-{i+1:03}", "GB", "LIGHT-A19",
                    "B-2409" if i < 22 else "B-2408", "配网失败" if i < 20 else ("运输破损" if i < 25 else "不再需要"),
                    (stamp-timedelta(days=i % 25)).isoformat(timespec="seconds")))
            db.executemany("INSERT INTO batches VALUES (?,?,?,?)", [
                ("B-2409", "1.8.2", 140, "模拟记录：出厂抽检未覆盖英国双频路由器配网场景；需要复现测试"),
                ("B-2408", "1.8.1", 190, "模拟记录：常规出厂抽检通过")])
            self.audit(db, "system.initialized", "MVP", {"mode": "simulation", "orders": 36, "tickets": 240}, "系统")

