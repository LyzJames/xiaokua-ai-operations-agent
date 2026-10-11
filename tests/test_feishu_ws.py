"""Exercise the official SDK dispatch boundary without contacting Feishu."""
import json
import pytest
from backend.production.feishu_ws import build_handler
from backend.production.feishu import FeishuError
import lark_oapi as lark

class Database:
    def __init__(self):
        self.connection={"id":"conn_test","tenant_id":"tenant_test","provider":"feishu","status":"active","info":{"delivery":"websocket"}}
    def get(self,table,id,tenant):
        assert id=="conn_test" and tenant=="tenant_test"
        return self.connection

class Receiver:
    def __init__(self):self.values=[]
    def receive(self,connection,value):
        self.values.append(value)
        return {"toast":{"type":"info","content":"queued"}} if value["header"]["event_type"]=="card.action.trigger" else {"code":0}

def payload(kind,app="cli_test"):
    return json.dumps({"schema":"2.0","header":{"event_id":"evt_test","event_type":kind,"app_id":app,"tenant_key":"tenant"},
        "event":{"operator":{"open_id":"ou_test"},"action":{"value":{"action_id":"action_test","decision":"approve"}},
                 "sender":{"sender_id":{"open_id":"ou_test"},"sender_type":"user"},
                 "message":{"message_id":"msg_test","message_type":"text","content":"{\"text\":\"同步订单\"}"}}}).encode()

def test_sdk_dispatches_message_and_modern_card():
    db=Database();receiver=Receiver();handler=build_handler(db,receiver,db.connection,"cli_test")
    handler._do_without_validation(payload("im.message.receive_v1"))
    response=handler._do_without_validation(payload("card.action.trigger"))
    assert json.loads(lark.JSON.marshal(response))["toast"]["content"]=="queued"
    assert [v["header"]["event_type"] for v in receiver.values]==["im.message.receive_v1","card.action.trigger"]
    assert receiver.values[1]["event"]["action"]["value"]["decision"]=="approve"

def test_ws_rechecks_connection_and_application_identity():
    db=Database();receiver=Receiver();handler=build_handler(db,receiver,db.connection,"cli_test")
    with pytest.raises(FeishuError):handler._do_without_validation(payload("im.message.receive_v1","cli_other"))
    db.connection["status"]="disabled"
    with pytest.raises(FeishuError):handler._do_without_validation(payload("im.message.receive_v1"))
    response=handler._do_without_validation(payload("card.action.trigger"))
    assert json.loads(lark.JSON.marshal(response))["toast"]["type"]=="error"
    assert receiver.values==[]
    db.connection["status"]="active";db.connection["info"]["delivery"]="https-webhook"
    with pytest.raises(FeishuError):handler._do_without_validation(payload("im.message.receive_v1"))

def test_ws_unbound_card_returns_error_without_queuing():
    db=Database()
    class Unbound:
        def receive(self,connection,value):raise FeishuError("操作者未绑定本组织成员")
    handler=build_handler(db,Unbound(),db.connection,"cli_test")
    response=handler._do_without_validation(payload("card.action.trigger"))
    assert json.loads(lark.JSON.marshal(response))["toast"]["type"]=="error"
