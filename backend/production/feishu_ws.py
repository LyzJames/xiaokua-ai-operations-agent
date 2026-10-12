"""Authenticated SDK long connection for Feishu messages and card callbacks.

This is an explicit process per organization, not an unauthenticated HTTP bypass.
Both transports share the same member binding, durable queue and approval checks.
"""
import argparse
import json
import lark_oapi as lark
from .api import build_services
from .database import connections
from .feishu import FeishuError
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

def build_handler(db,feishu,connection,app_id):
    def receive(data):
        current=db.get(connections,connection["id"],connection["tenant_id"])
        if not current or current["provider"]!="feishu" or current["status"]!="active" or current["info"].get("delivery")!="websocket":
            raise FeishuError("飞书连接已停用")
        value=json.loads(lark.JSON.marshal(data))
        if value.get("header",{}).get("app_id")!=app_id:
            raise FeishuError("长连接事件应用身份不匹配")
        # The SDK authenticates the transport. Business authorization still happens
        # in receive() and again when the persistent worker processes the event.
        return feishu.receive(current,value)
    def card(data):
        try:
            result=receive(data)
            if "toast" not in result:
                result={"toast":{"type":"info","content":"此请求已接收，请在工作台查看状态。"}}
        except FeishuError:
            result={"toast":{"type":"error","content":"连接不可用或成员身份未绑定，请在工作台检查。"}}
        return P2CardActionTriggerResponse(result)
    return (lark.EventDispatcherHandler.builder("","")
            .register_p2_im_message_receive_v1(receive)
            .register_p2_card_action_trigger(card).build())

def main():
    parser=argparse.ArgumentParser(description="组织飞书长连接消息及卡片回调入口")
    parser.add_argument("--connection-id",required=True)
    args=parser.parse_args()
    db,vault,ops,feishu,settings=build_services()
    con=db.get(connections,args.connection_id)
    if not con or con["provider"]!="feishu" or con["status"]!="active" or con["info"].get("delivery")!="websocket":raise SystemExit("请选择已在工作台验证并使用长连接方式的飞书连接")
    secret=vault.open(con["secret"])
    handler=build_handler(db,feishu,con,secret["app_id"])
    ws=lark.ws.Client(secret["app_id"],secret["app_secret"],event_handler=handler,log_level=lark.LogLevel.ERROR)
    print("飞书长连接正在启动；仅已绑定成员可创建任务和提交卡片审批，结果由持久化作业回传。",flush=True)
    try:ws.start()
    finally:db.engine.dispose()

if __name__=="__main__":main()
