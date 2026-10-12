import hashlib
import hmac
import json
import time
import httpx
from sqlalchemy import select, update
from .database import connections, users, tasks, actions, events, clock, uid, row
from .security import ROLES
from .workflows import BusinessError, redact

class FeishuError(Exception): pass

def status_label(status):
    return {"needs_input":"请补充指令","completed":"已完成","executed":"已执行","rejected":"已拒绝","uncertain":"结果待核实","needs_review":"需要核实","failed":"失败","partial":"部分完成","reconciled":"已核实结案","expired":"已过期","approved":"已批准，等待执行","pending":"等待审批","awaiting_approval":"等待审批","queued":"排队中","running":"处理中"}.get(status,"处理中")

def approval_preview(action):
    """Plain text keeps product/customer text from becoming card markup."""
    p=action["payload"];before=action["before"];kind=action["kind"]
    if kind == "pool_adjust":
        return f"共享库存池：{p['pool_id']}\n实物在库：{before['on_hand']} → {p['quantity']}\n安全库存：{before['safety']} → {p['safety']}"
    if kind in ("sandbox_sku", "sandbox_stock", "mcf_create", "mcf_cancel", "sandbox_status", "sandbox_package"):
        return "Amazon 动态沙箱操作\n" + json.dumps({key:value for key,value in p.items() if key in {"sku","quantity","country","order_id","package_id","status"}}, ensure_ascii=False)
    if kind=="inventory":
        return f"库存数量：{before['quantity']} → {p['quantity']}\n库存项目：{p['item_id']}\n库存地点：{p['location_id']}"
    if kind=="product":
        import html,re
        old=html.unescape(re.sub(r"<[^>]*>"," ",before["description_html"]))
        return "原说明："+redact(old)[:700]+"\n新说明："+redact(p["description"])[:700]+"\n完整说明请在工作台核对。"
    text="订单："+p["order_id"]
    if kind=="refund":text+=f"\n退款金额：{p['amount_cents']//100}.{p['amount_cents']%100:02d} {p['currency']}"
    elif kind in ("fulfill","tracking"):text+="\n物流公司："+p["company"]+"\n单号："+p["number"]
    else:text+="\n取消原因："+{"CUSTOMER":"客户请求","FRAUD":"欺诈","INVENTORY":"库存不足","DECLINED":"付款被拒","OTHER":"其他"}.get(p.get("reason","OTHER"),"其他")+"\n此操作不退款。"
    return text

def feishu_request(method, path, token=None, payload=None, params=None):
    if not path.startswith("/") or ".." in path: raise FeishuError("接口路径无效")
    try:
        with httpx.Client(timeout=20,trust_env=False) as c:
            r=c.request(method,"https://open.feishu.cn/open-apis"+path,headers={"Authorization":"Bearer "+token} if token else {},json=payload,params=params)
            j=r.json()
            if r.status_code!=200 or j.get("code",0)!=0: raise FeishuError("飞书接口返回错误 "+str(j.get("code",r.status_code)))
            return j
    except (httpx.HTTPError,ValueError): raise FeishuError("飞书接口暂不可用") from None

def tenant_token(secret):
    j=feishu_request("POST","/auth/v3/tenant_access_token/internal",payload={"app_id":secret["app_id"],"app_secret":secret["app_secret"]})
    return j["tenant_access_token"]

def verify_event(body, headers, secret):
    token=secret.get("verification_token","");key=secret.get("encrypt_key","")
    if not token or not key: raise FeishuError("必须配置 Verification Token 和 Encrypt Key")
    value=json.loads(body)
    if value.get("encrypt"):
        # The official SDK implements Feishu's AES-CBC envelope and padding convention.
        from lark_oapi.core.utils import AESCipher
        value=json.loads(AESCipher(key).decrypt_str(value["encrypt"]))
    if value.get("type")=="url_verification":
        if not hmac.compare_digest(value.get("token",""),token): raise FeishuError("回调校验失败")
        return value
    timestamp=headers.get("x-lark-request-timestamp","");nonce=headers.get("x-lark-request-nonce","")
    try:
        if abs(clock()-float(timestamp))>300: raise ValueError()
    except ValueError: raise FeishuError("回调时间戳无效") from None
    expected=hashlib.sha256((timestamp+nonce+key).encode()+body).hexdigest()
    if not hmac.compare_digest(headers.get("x-lark-signature",""),expected): raise FeishuError("回调签名无效")
    header=value.get("header",{})
    if not hmac.compare_digest(header.get("token",""),token) or header.get("app_id")!=secret["app_id"]: raise FeishuError("回调身份无效")
    return value

class Feishu:
    def __init__(self,db,vault,operations,settings): self.db,self.vault,self.ops,self.settings=db,vault,operations,settings

    def token(self,secret):
        return tenant_token(secret)

    def receive(self,conn,value):
        t=conn["tenant_id"];header=value.get("header",{});event=value.get("event",{});typ=header.get("event_type","")
        eid=header.get("event_id")
        if not isinstance(eid,str) or not eid or len(eid)>200: raise FeishuError("事件标识无效")
        if conn["info"].get("tenant_key") and header.get("tenant_key")!=conn["info"]["tenant_key"]: raise FeishuError("事件租户不匹配")
        with self.db.transaction() as db:
            old=db.execute(select(events.c.id).where(events.c.provider=="feishu",events.c.connection_id==conn["id"],events.c.external_id==eid)).first()
            if old: return {"code":0}
            if typ=="card.action.trigger":
                sender=event.get("operator",{}).get("open_id")
            elif typ=="im.message.receive_v1":
                sender=event.get("sender",{}).get("sender_id",{}).get("open_id")
                if event.get("sender",{}).get("sender_type")!="user": return {"code":0}
            else: return {"code":0}
            actor=row(db.execute(select(users).where(users.c.tenant_id==t,users.c.feishu_open_id==sender,users.c.active==True)).first())
            if not actor or ROLES.get(actor["role"],0)<2: raise FeishuError("操作者未绑定本组织成员或权限不足")
            minimal={"sender_id":sender,"event_type":typ}
            if typ=="card.action.trigger": minimal["value"]=event.get("action",{}).get("value",{})
            else:
                msg=event.get("message",{})
                if msg.get("message_type")!="text": return {"code":0}
                try: text=json.loads(msg.get("content","{}"))["text"]
                except (ValueError,KeyError): raise FeishuError("消息格式无效") from None
                if not isinstance(text,str) or len(text)>2000: raise FeishuError("消息长度超限")
                minimal.update(text=redact(text),message_id=msg.get("message_id"))
            record=dict(id=uid("event"),provider="feishu",external_id=eid,connection_id=conn["id"],topic=typ,payload=minimal,status="queued",created_at=clock())
            db.execute(events.insert().values(**record))
            self.db.enqueue(db,t,"feishu_event",record["id"])
            self.db.audit(db,t,actor["id"],"feishu.received",record["id"],{"type":typ})
        if typ=="card.action.trigger": return {"toast":{"type":"info","content":"已接收审批请求，将校验角色与操作状态。"}}
        return {"code":0}

    def event_job(self,event_id,tenant_id):
        ev=self.db.get(events,event_id)
        conn=self.db.get(connections,ev["connection_id"],tenant_id)
        if not conn or conn["provider"]!="feishu" or conn["status"]!="active": raise FeishuError("飞书连接不可用")
        p=ev["payload"]
        with self.db.engine.connect() as db:
            actor=row(db.execute(select(users).where(users.c.tenant_id==tenant_id,users.c.feishu_open_id==p["sender_id"],users.c.active==True)).first())
        if not actor or ROLES.get(actor["role"],0)<2: raise FeishuError("操作者权限已撤销")
        secret=self.vault.open(conn["secret"])
        if ev["topic"]=="card.action.trigger":
            if actor["role"] not in ("owner","approver"): raise FeishuError("无审批权限")
            value=p["value"]
            decision=value.get("decision")
            if decision not in ("approve","reject"): raise FeishuError("审批值无效")
            try:
                self.ops.decide(actor,value.get("action_id"),decision=="approve","飞书卡片审批")
                text="审批已处理，可在工作台查看实际执行结果。"
            except BusinessError as e: text=str(e)
        else:
            text=p["text"].strip()
            from .commands import submit_command
            from .platforms.contracts import PROVIDERS
            import re
            shops = [shop for shop in self.db.list(connections, tenant_id, 100, status="active") if shop["provider"] in PROVIDERS]
            labels = re.findall(r"店铺[\[【]([^\]】]{1,120})[\]】]", text)
            if any(word in text for word in ("所有店铺", "全部店铺")):
                selected = [shop["id"] for shop in shops]
            elif labels:
                selected = [shop["id"] for shop in shops if any(label in (shop["id"], shop["external_id"], shop["info"].get("label"), shop["info"].get("shop", {}).get("name")) for label in labels)]
                if len(selected) != len(labels):
                    selected = []
            else:
                mentioned = [shop for shop in shops if PROVIDERS[shop["provider"]].name.lower() in text.lower()]
                selected = [mentioned[0]["id"]] if len(mentioned) == 1 else [shops[0]["id"]] if len(shops) == 1 else []
            if not selected:
                text = "请明确店铺范围，例如‘同步所有店铺’或‘店铺[你的店铺标签] 分析订单’。也可在 Web 选择店铺。" + self.settings.public_url
            else:
                task = submit_command(self.ops, actor, text, selected, "feishu", "feishu-"+ev["external_id"],
                    {"notify_open_id":p["sender_id"],"notify_feishu_connection":conn["id"]}, use_ai=not getattr(self.ops, "offline", False))
                text=f"已创建任务 {task['id']}。写入仍需独立审批。结果与审批：{self.settings.public_url}"
        token=self.token(secret)
        self.send(token,p["sender_id"],"text",{"text":text},ev["id"])
        with self.db.transaction() as db:
            db.execute(update(events).where(events.c.id==event_id).values(status="processed"))

    def send(self,token,open_id,msg_type,content,key):
        return feishu_request("POST","/im/v1/messages",token,{"receive_id":open_id,"msg_type":msg_type,"content":json.dumps(content,ensure_ascii=False),"uuid":hashlib.sha256(key.encode()).hexdigest()[:32]}, {"receive_id_type":"open_id"})

    def notify(self,action_id,tenant_id,result=False):
        a=self.db.get(actions,action_id,tenant_id)
        if not a: return
        cons=self.db.list(connections,tenant_id,10,provider="feishu",status="active")
        if not cons: return
        with self.db.engine.connect() as db:
            if result: query=select(users).where(users.c.id.in_([a["actor_id"],a["approved_by"]]),users.c.tenant_id==tenant_id,users.c.active==True)
            else: query=select(users).where(users.c.tenant_id==tenant_id,users.c.active==True,users.c.id!=a["actor_id"],users.c.role.in_(["owner","approver"]))
            recipients=[row(r) for r in db.execute(query) if r._mapping["feishu_open_id"]]
        secret=self.vault.open(cons[0]["secret"]);token=self.token(secret)
        for u in recipients:
            if result:
                self.send(token,u["feishu_open_id"],"text",{"text":f"{a['title']}：{status_label(a['status'])}。{self.settings.public_url}"},action_id+"result"+u["id"])
            else:
                card={"config":{"wide_screen_mode":True},"header":{"title":{"tag":"plain_text","content":"小跨 · 待审批操作"}},"elements":[
                    {"tag":"div","text":{"tag":"plain_text","content":approval_preview(a)}},
                    {"tag":"markdown","content":f"**{a['title']}**\n操作 ID：{a['id']}\n请在工作台核对前后差异：{self.settings.public_url}"},
                    {"tag":"action","actions":[{"tag":"button","text":{"tag":"plain_text","content":"批准"},"type":"primary","value":{"action_id":a["id"],"decision":"approve"}},
                    {"tag":"button","text":{"tag":"plain_text","content":"拒绝"},"value":{"action_id":a["id"],"decision":"reject"}}]}]}
                self.send(token,u["feishu_open_id"],"interactive",card,action_id+u["id"])

    def notify_task(self,task_id,tenant_id):
        notification_key = task_id
        task_id, _, requested_status = task_id.partition(":")
        task=self.db.get(tasks,task_id,tenant_id)
        if not task:return
        if requested_status and task["status"] != requested_status:
            return {"ok": True, "superseded": True}
        p=task["params"];con=self.db.get(connections,p.get("notify_feishu_connection"),tenant_id)
        actor=self.db.get(users,task["actor_id"],tenant_id)
        if not con or con["provider"]!="feishu" or con["status"]!="active" or not actor or not actor["active"] or actor["feishu_open_id"]!=p.get("notify_open_id"):raise FeishuError("通知接收人或连接权限已失效")
        text=f"任务 {task_id}：{status_label(task['status'])}\n{task['result'].get('summary','')}"
        for r in task["result"].get("reports",[]):text+=f"\n\n{r['role']}\n{r['finding'][:900]}"
        text+="\n\n工作台："+self.settings.public_url
        self.send(self.token(self.vault.open(con["secret"])),actor["feishu_open_id"],"text",{"text":text},notification_key+"complete")
