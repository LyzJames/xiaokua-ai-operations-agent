import json
import re
import httpx
from .config import MODEL, MODEL_URL, credentials

class IntegrationError(Exception):
    pass

def model_json(system: str, content: str) -> dict:
    key = credentials().get("DEEPSEEK_API_KEY")
    if not key:
        raise IntegrationError("未配置 DeepSeek API Key")
    try:
        with httpx.Client(timeout=50, trust_env=False) as client:
            response = client.post(f"{MODEL_URL}/chat/completions", headers={"Authorization": f"Bearer {key}"}, json={
                "model": MODEL, "messages": [{"role": "system", "content": system + " 请仅输出 JSON 对象。"},
                                               {"role": "user", "content": content}],
                "response_format": {"type": "json_object"}, "max_tokens": 1400,
                "thinking": {"type": "disabled"}, "stream": False})
            response.raise_for_status()
            result = json.loads(response.json()["choices"][0]["message"]["content"])
            if not isinstance(result, dict):
                raise ValueError("object required")
            return result
    except Exception as error:
        # Do not propagate response bodies, URLs with tokens, or Authorization headers.
        raise IntegrationError(f"模型暂不可用（{type(error).__name__}），已使用明确标记的规则方案") from None

def integration_status():
    keys = credentials()
    return [
        {"id": "deepseek", "name": "DeepSeek V4.1 Flash", "category": "智能推理", "configured": bool(keys.get("DEEPSEEK_API_KEY")),
         "mode": "available", "detail": f"模型 {MODEL} · 可在创建任务时开启 AI 推理"},
        {"id": "feishu", "name": "飞书", "category": "办公入口", "configured": all(keys.get(k) for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET")),
         "mode": "reserved", "detail": "凭据校验与长连接机器人已实现；需配置事件订阅、消息权限与操作者白名单"},
        {"id": "shopify", "name": "Shopify", "category": "订单与库存", "configured": bool(keys.get("SHOPIFY_CLIENT_ID") and keys.get("SHOPIFY_CLIENT_SECRET")),
         "mode": "reserved", "detail": "只读接口已预留；" + ("已配置店铺域名" if keys.get("SHOPIFY_SHOP_DOMAIN") else "待补充 *.myshopify.com 店铺域名")},
        {"id": "amazon", "name": "Amazon SP-API", "category": "销售渠道", "configured": False, "mode": "simulation", "detail": "当前为模拟数据，待配置卖家授权与应用角色"},
        {"id": "meta", "name": "Meta Ads", "category": "广告投放", "configured": False, "mode": "simulation", "detail": "当前为模拟投放，待配置广告账户与 ads_management 权限"},
        {"id": "payment", "name": "支付服务商", "category": "采购结算", "configured": False, "mode": "simulation", "detail": "仅模拟钱包账本，待选择支付服务商并完成商户接入"},
    ]

def shopify_readonly(operation="health"):
    keys = credentials()
    domain = keys.get("SHOPIFY_SHOP_DOMAIN", "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", domain):
        raise IntegrationError("请在 .env 中补充 SHOPIFY_SHOP_DOMAIN，格式为 store.myshopify.com")
    try:
        with httpx.Client(timeout=20, trust_env=False) as client:
            token = keys.get("SHOPIFY_ACCESS_TOKEN")
            if not token:
                response = client.post(f"https://{domain}/admin/oauth/access_token", data={"grant_type": "client_credentials",
                    "client_id": keys.get("SHOPIFY_CLIENT_ID", ""), "client_secret": keys.get("SHOPIFY_CLIENT_SECRET", "")})
                response.raise_for_status()
                token = response.json()["access_token"]
            query = "{shop{name}}" if operation == "health" else "{orders(first:10,sortKey:CREATED_AT,reverse:true){nodes{id name createdAt displayFulfillmentStatus totalPriceSet{shopMoney{amount currencyCode}}}}}"
            response = client.post(f"https://{domain}/admin/api/2026-10/graphql.json",
                headers={"X-Shopify-Access-Token": token}, json={"query": query})
            response.raise_for_status()
            result = response.json()
            if result.get("errors"):
                raise IntegrationError("Shopify 授权或查询失败，请检查应用安装与 read_orders 权限")
            return result["data"]
    except IntegrationError:
        raise
    except Exception as error:
        raise IntegrationError(f"Shopify 连接失败（{type(error).__name__}），请检查授权") from None

def test_connection(name):
    keys = credentials()
    if name == "deepseek":
        result = model_json("你是连接测试助手。", '返回 {"status":"ok"}')
        return {"ok": result.get("status") == "ok", "message": f"{MODEL} 已完成实际推理测试"}
    if name == "shopify":
        shopify_readonly()
        return {"ok": True, "message": "Shopify 只读连接成功，模拟业务数据保持独立"}
    if name == "feishu":
        try:
            with httpx.Client(timeout=20, trust_env=False) as client:
                response = client.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", json={
                    "app_id": keys.get("FEISHU_APP_ID", ""), "app_secret": keys.get("FEISHU_APP_SECRET", "")})
                response.raise_for_status()
                data = response.json()
                if data.get("code") != 0:
                    return {"ok": False, "message": "飞书凭据校验失败，请检查应用状态"}
            return {"ok": True, "message": "飞书应用凭据有效；机器人消息权限与事件订阅需在开放平台启用"}
        except Exception as error:
            raise IntegrationError(f"飞书网络连接失败（{type(error).__name__}）") from None
    raise IntegrationError("该平台当前仅提供模拟适配器")

