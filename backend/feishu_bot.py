"""Optional Feishu long-connection entry point; never starts automatically."""
import json
import os
import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, ReplyMessageRequest, ReplyMessageRequestBody
from .config import DATA_DIR, credentials

def main():
    config = credentials()
    allowlist = {v.strip() for v in os.getenv("FEISHU_ALLOWED_OPEN_IDS", "").split(",") if v.strip()}
    if not config.get("FEISHU_APP_ID") or not config.get("FEISHU_APP_SECRET"):
        raise SystemExit("请先配置飞书 App ID 与 App Secret")
    if not allowlist:
        raise SystemExit("请先配置 FEISHU_ALLOWED_OPEN_IDS 操作者白名单，再启动机器人")
    token_path = DATA_DIR / "service.token"
    if not token_path.is_file():
        raise SystemExit("请先启动小跨后端服务")
    service_token = token_path.read_text(encoding="utf-8").strip()
    client = lark.Client.builder().app_id(config["FEISHU_APP_ID"]).app_secret(config["FEISHU_APP_SECRET"]).log_level(lark.LogLevel.ERROR).build()

    def receive(data: P2ImMessageReceiveV1):
        try:
            sender = data.event.sender.sender_id.open_id
            message = data.event.message
            if sender not in allowlist or message.message_type != "text":
                return
            text = json.loads(message.content).get("text", "").strip()
            if not text:
                return
            for mention in message.mentions or []:
                text = text.replace(mention.key, "").strip()
            with httpx.Client(timeout=15, trust_env=False) as local:
                response = local.post("http://127.0.0.1:8000/api/bot/tasks", headers={"X-Xiaokua-Service": service_token}, json={
                    "event_id": data.header.event_id, "sender_id": sender, "command": text})
                response.raise_for_status()
                result = response.json()
            request = ReplyMessageRequest.builder().message_id(message.message_id).request_body(
                ReplyMessageRequestBody.builder().msg_type("text").content(json.dumps({"text": result["message"]}, ensure_ascii=False)).build()).build()
            reply = client.im.v1.message.reply(request)
            if not reply.success():
                print("机器人已接收任务，但消息回复失败，请检查消息权限。")
        except Exception as error:
            print(f"机器人事件处理失败（{type(error).__name__}），详情请检查本机服务状态。")

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(receive).build()
    ws = lark.ws.Client(config["FEISHU_APP_ID"], config["FEISHU_APP_SECRET"], event_handler=handler, log_level=lark.LogLevel.ERROR)
    print("小跨飞书入口已启动：仅白名单操作者可创建模拟任务；审批在本机工作台完成。")
    ws.start()

if __name__ == "__main__":
    main()
