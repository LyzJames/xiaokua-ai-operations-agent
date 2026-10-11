import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import pytest
from backend.engine import Engine, cents
from backend.store import Store, TZ

@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path)
    engine = Engine(store)
    yield store, engine
    engine.close()

def run(env, kind, params=None, command=None):
    store, engine = env
    task = engine.submit(command or kind, kind=kind, params=params, enqueue=False)
    engine.run(task["id"])
    return store.task(task["id"])

@pytest.mark.parametrize("value", ["NaN", "Infinity", "0", "-1", "1.001", "100001", "hello", True])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        cents(value)

def test_exact_cents():
    assert cents("0.29") == 29
    assert cents("50.00") == 5000

def test_order_and_inventory_consistency(env):
    store, _ = env
    task = run(env, "orders")
    assert task["status"] == "completed"
    assert len([a for a in task["actions"] if a["kind"] == "fulfill"]) == 10
    assert sum(i["reserved"] for i in store.rows("SELECT * FROM inventory")) == 10
    assert not store.rows("SELECT * FROM orders WHERE status='pending' AND country IN ('DE','FR','GB')")
    snapshot = store.setting("inventory_snapshot")["items"]
    stock = {r["sku"]: r["stock"] - r["reserved"] for r in store.rows("SELECT * FROM inventory")}
    assert all(r["available"] == stock[r["sku"]] for r in snapshot)
    run(env, "orders")
    assert sum(i["reserved"] for i in store.rows("SELECT * FROM inventory")) == 10

def test_low_risk_actions_execute_while_ads_wait(env):
    store, engine = env
    task = engine.submit("处理欧洲订单，并优化广告", enqueue=False)
    engine.run(task["id"])
    task = store.task(task["id"])
    assert task["status"] == "awaiting_approval"
    assert all(a["status"] == "executed" for a in task["actions"] if a["kind"] == "fulfill")
    assert all(a["status"] == "pending" for a in task["actions"] if a["kind"] == "ad_budget")

def test_approval_resume_survives_restart(env):
    store, engine = env
    task = run(env, "purchase", {"amount": "180", "vendor": "EcoPack EU"})
    assert task["status"] == "awaiting_approval"
    assert not store.rows("SELECT * FROM ledger")
    engine.close()
    restarted = Engine(store)
    try:
        restarted.decide(task["actions"][0]["id"], True, "采购需求已核验")
        restarted.run(task["id"])
        assert store.task(task["id"])["status"] == "completed"
        assert store.one("SELECT SUM(amount_cents) AS amount FROM ledger")["amount"] == 18000
        restarted.run(task["id"])
        assert len(store.rows("SELECT * FROM ledger")) == 1
        with pytest.raises(ValueError):
            restarted.decide(task["actions"][0]["id"], True)
        assert store.verify_audit()["valid"]
    finally:
        restarted.close()
    # The fixture closes the original engine again; SQLite close is idempotent.

def test_refund_split_cannot_bypass_order_limit(env):
    store, _ = env
    first = run(env, "refund", {"amount": "30", "order_id": "EU-1043"})
    second = run(env, "refund", {"amount": "25", "order_id": "EU-1043"})
    assert first["status"] == "completed"
    assert second["status"] == "awaiting_approval"
    assert store.one("SELECT refunded_cents FROM orders WHERE id='EU-1043'")["refunded_cents"] == 3000

def test_refund_cannot_exceed_paid_amount(env):
    task = run(env, "refund", {"amount": "100", "order_id": "EU-1043"})
    assert task["actions"][0]["status"] == "blocked"
    assert not env[0].rows("SELECT * FROM ledger")

def test_daily_purchase_cap_and_approval(env):
    store, engine = env
    for _ in range(3):
        assert run(env, "purchase", {"amount": "100"})["status"] == "completed"
    fourth = run(env, "purchase", {"amount": "1"})
    assert fourth["status"] == "awaiting_approval"
    assert sum(r["amount_cents"] for r in store.rows("SELECT * FROM ledger")) == 30000
    engine.decide(fourth["actions"][0]["id"], False)
    engine.run(fourth["id"])
    assert store.task(fourth["id"])["status"] == "rejected"

def test_non_whitelisted_vendor_blocked(env):
    task = run(env, "purchase", {"amount": "10", "vendor": "Unknown"})
    assert task["status"] == "rejected"
    assert task["actions"][0]["status"] == "blocked"
    with pytest.raises(ValueError):
        env[1].decide(task["actions"][0]["id"], True)

def test_approved_purchase_still_checks_balance(env):
    store, engine = env
    task = run(env, "purchase", {"amount": "2400"})
    with store.transaction() as db:
        store.set_setting("wallet_initial_cents", 10000, db)
    engine.decide(task["actions"][0]["id"], True)
    engine.run(task["id"])
    assert store.task(task["id"])["actions"][0]["status"] == "blocked"
    assert not store.rows("SELECT * FROM ledger")

def test_approval_expiration(env):
    store, engine = env
    task = run(env, "purchase", {"amount": "180"})
    with store.transaction() as db:
        db.execute("UPDATE actions SET expires_at=? WHERE task_id=?", ((datetime.now(TZ) - timedelta(hours=1)).isoformat(), task["id"]))
    with pytest.raises(ValueError):
        engine.decide(task["actions"][0]["id"], True)
    engine.expire_actions()
    engine.run(task["id"])
    assert store.task(task["id"])["actions"][0]["status"] == "expired"
    assert not store.rows("SELECT * FROM ledger")

def test_idempotency_under_concurrent_requests(env):
    store, engine = env
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: engine.submit("采购", "purchase", params={"amount": "80"}, idem="one-operation", enqueue=False)["id"], range(4)))
    assert len(set(ids)) == 1
    engine.run(ids[0])
    assert len(store.rows("SELECT * FROM ledger")) == 1

def test_memory_skills_are_durable_and_idempotent(env):
    store, _ = env
    run(env, "memory")
    run(env, "memory")
    assert len(store.rows("SELECT * FROM memories")) == 2
    skills = store.rows("SELECT * FROM skills")
    assert len(skills) == 2
    assert "不得承诺" in next(s for s in skills if "法国" in s["title"])["content"]
    assert all(len(json.loads(m["evidence"])) > 0 for m in store.rows("SELECT * FROM memories"))
    reopened = Store(store.directory)
    assert len(reopened.rows("SELECT * FROM skills")) == 2

def test_incident_parallel_reports_and_pause_requires_approval(env):
    store, engine = env
    task = run(env, "incident")
    assert task["status"] == "awaiting_approval"
    assert {r["key"] for r in task["result"]["reports"]} == {"data", "supply", "marketing"}
    assert store.one("SELECT status FROM campaigns WHERE id='AD-GB-01'")["status"] == "active"
    engine.decide(task["actions"][0]["id"], True)
    engine.run(task["id"])
    assert store.one("SELECT status FROM campaigns WHERE id='AD-GB-01'")["status"] == "paused"
    assert "尚未确认根因" in store.task(task["id"])["result"]["summary"]

def test_stale_ad_budget_is_blocked_after_approval(env):
    store, engine = env
    task = run(env, "ads")
    with store.transaction() as db:
        db.execute("UPDATE campaigns SET budget_cents=9999 WHERE id='AD-DE-01'")
    for action in task["actions"]:
        engine.decide(action["id"], True)
    engine.run(task["id"])
    updated = store.task(task["id"])
    assert updated["status"] == "partial"
    assert any(a["status"] == "blocked" for a in updated["actions"])
    assert store.one("SELECT budget_cents FROM campaigns WHERE id='AD-DE-01'")["budget_cents"] == 9999

def test_append_only_audit_and_hash_chain(env):
    store, _ = env
    run(env, "purchase")
    assert store.verify_audit()["valid"]
    with pytest.raises(Exception):
        with store.transaction() as db:
            db.execute("DELETE FROM audit")
    with pytest.raises(Exception):
        with store.transaction() as db:
            db.execute("UPDATE audit SET actor='bad'")



def test_skill_recall_is_relevant_to_market(env):
    store, _ = env
    run(env, "memory")
    task = run(env, "auto", command="德国客户询问能效该如何答复")
    assert "已复用技能" in task["result"]["summary"]
    assert "能效标签" in task["result"]["summary"]
    assert "不推断" in task["result"]["summary"]
    assert store.one("SELECT uses FROM skills WHERE id='skill-de-energy'")["uses"] == 1
    assert store.one("SELECT uses FROM skills WHERE id='skill-fr-local_return'")["uses"] == 0
    run(env, "incident")
    assert store.one("SELECT uses FROM skills WHERE id='skill-de-energy'")["uses"] == 1


def test_procurement_selects_allowed_lowest_quote(env):
    task = run(env, "purchase", {"amount": "80", "vendor": "auto"})
    assert task["status"] == "completed"
    assert task["actions"][0]["payload"]["vendor"] == "EcoPack EU"
    assert any(trace["node"] == "quotes" for trace in task["traces"])
