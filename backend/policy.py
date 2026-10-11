"""All money is integer cents; policy is applied again inside the execution transaction."""
from datetime import datetime
from .store import TZ

def evaluate(db, action: dict, policy: dict, initial_balance: int, approved=False):
    kind, payload = action["kind"], action["payload"]
    if kind in ("ad_budget", "ad_pause"):
        row = db.execute("SELECT * FROM campaigns WHERE id=?", (payload["campaign_id"],)).fetchone()
        if not row:
            return "blocked", "广告组不存在"
        if kind == "ad_budget" and row["budget_cents"] != payload["previous_budget_cents"]:
            return "blocked", "广告预算已变化，请重新生成方案"
        return ("ready", "人工已批准广告调整") if approved else ("pending", "广告调整默认由负责人审批")
    if kind not in ("refund", "purchase"):
        return "ready", "非资金操作"
    amount = payload.get("amount_cents", 0)
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0 or amount > 10000000:
        return "blocked", "金额不合法"
    if payload.get("currency") != "USD":
        return "blocked", "首版仅支持 USD 额度控制"
    if kind == "purchase":
        if payload.get("vendor") not in policy["vendor_whitelist"]:
            return "blocked", "供应商不在白名单，须先由负责人完成供应商准入"
        spent = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE kind='purchase'").fetchone()[0]
        if amount > initial_balance - spent:
            return "blocked", "模拟钱包余额不足"
    else:
        order = db.execute("SELECT * FROM orders WHERE id=?", (payload.get("order_id"),)).fetchone()
        if not order:
            return "blocked", "退款订单不存在"
        if amount > order["total_cents"] - order["refunded_cents"]:
            return "blocked", "退款不能超过订单剩余可退金额"
        if order["refunded_cents"] + amount > policy["refund_single_cents"] and not approved:
            return "pending", "同一订单累计退款超过 $50 自动执行上限"
    day = datetime.now(TZ).date().isoformat()
    spent_today = db.execute("SELECT COALESCE(SUM(amount_cents),0) FROM ledger WHERE kind=? AND substr(created_at,1,10)=?", (kind, day)).fetchone()[0]
    if not approved and amount > policy[f"{kind}_single_cents"]:
        return "pending", "超过单笔自动执行上限"
    if not approved and spent_today + amount > policy[f"{kind}_daily_cents"]:
        return "pending", "超过每日自动执行总额"
    return "ready", "人工批准，硬性校验通过" if approved else "在自动执行额度内"

