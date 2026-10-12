import os
import threading
import time
from sqlalchemy import select, update, or_, case
from .database import jobs, actions, tasks, connections, events, resources, tenants, runtime_workers, clock, uid, row
from .workflows import BusinessError
from .shopify import ShopifyError
from .platforms.contracts import PlatformError, PROVIDERS
from .feishu import FeishuError

class Worker:
    def __init__(self,db,ops,feishu):
        self.db,self.ops,self.feishu=db,ops,feishu
        self.identity=uid("worker");self.stop_event=threading.Event();self.thread=None
        self.last_schedule=0
    def start(self):
        self.thread=threading.Thread(target=self.run,daemon=True,name="xiaokua-durable-worker");self.thread.start()
    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=35)
    def recover(self):
        changed=[]
        with self.db.transaction() as db:
            stale=[row(r) for r in db.execute(select(jobs).where(jobs.c.status=="running",jobs.c.lease_until<clock()).with_for_update())]
            for j in stale:
                if j["kind"]=="action":
                    a=self.db.get(actions,j["target_id"],j["tenant_id"],db)
                    if a and a["status"]=="executing":
                        db.execute(update(actions).where(actions.c.id==a["id"]).values(status="uncertain",reason="执行进程中断，须核查平台结果"))
                        db.execute(update(tasks).where(tasks.c.id==a["task_id"]).values(status="needs_review"))
                        self.db.audit(db,j["tenant_id"],"worker","action.uncertain",a["id"],{"reason":"lease_expired"})
                        changed.append(a)
                db.execute(update(jobs).where(jobs.c.id==j["id"]).values(status="queued",worker=None))
            expired=[row(r) for r in db.execute(select(actions).where(actions.c.status.in_(["pending","approved"]),actions.c.expires_at<clock()))]
            for a in expired:
                db.execute(update(actions).where(actions.c.id==a["id"]).values(status="expired"))
                db.execute(update(tasks).where(tasks.c.id==a["task_id"]).values(status="expired"))
                self.db.audit(db,a["tenant_id"],"worker","approval.expired",a["id"],{})
                changed.append(a)
        for a in changed:
            self.ops.shared.action_outcome(self.db.get(actions,a["id"],a["tenant_id"]))
        for t in {a["tenant_id"] for a in changed}:
            self.ops.refresh_commands(t)

    def claim(self):
        stopped=[]
        with self.db.transaction() as db:
            query=select(jobs.c.id).where(jobs.c.status=="queued",jobs.c.available_at<=clock()).order_by(case((jobs.c.kind=="task",1),else_=0), jobs.c.created_at).limit(100)
            job = None
            identifiers = [value[0] for value in db.execute(query)]
            for identifier in identifiers:
                candidate = row(db.execute(select(jobs).where(jobs.c.id == identifier, jobs.c.status == "queued",
                    jobs.c.available_at <= clock()).with_for_update(skip_locked=True)).first())
                if not candidate:
                    continue
                if candidate["kind"] == "task":
                    task = self.db.get(tasks, candidate["target_id"], candidate["tenant_id"], db)
                    if not task or task["status"] not in ("queued", "running"):
                        db.execute(update(jobs).where(jobs.c.id == candidate["id"]).values(status="done"))
                        continue
                    dependencies = [self.db.get(tasks, identifier, candidate["tenant_id"], db) for identifier in task["params"].get("depends_on", [])]
                    if any(not value or value["status"] in ("failed", "rejected", "expired", "needs_review", "needs_input", "partial") for value in dependencies):
                        db.execute(update(tasks).where(tasks.c.id == task["id"]).values(status="failed", result={"summary": "前置任务未完成或需要核实，后续操作已停止。"}))
                        db.execute(update(jobs).where(jobs.c.id == candidate["id"]).values(status="failed", error="DEPENDENCY_NOT_SUCCESSFUL"))
                        self.db.audit(db,candidate["tenant_id"],"worker","task.dependency.stopped",task["id"],{})
                        stopped.append(task)
                        continue
                    if any(value["status"] != "completed" for value in dependencies):
                        db.execute(update(jobs).where(jobs.c.id==candidate["id"]).values(available_at=clock()+2))
                        continue
                job = candidate
                break
            if job:
                db.execute(update(jobs).where(jobs.c.id==job["id"]).values(status="running",worker=self.identity,lease_until=clock()+90,attempts=job["attempts"]+1))
        for task in stopped:
            self.ops.shared.action_outcome({"tenant_id":task["tenant_id"],"task_id":task["id"],"status":"failed"})
        for t in {task["tenant_id"] for task in stopped}:
            self.ops.refresh_commands(t)
        return job

    def heartbeat(self,job_id,stop):
        while not stop.wait(20):
            with self.db.transaction() as db:
                db.execute(update(jobs).where(jobs.c.id==job_id,jobs.c.worker==self.identity,jobs.c.status=="running").values(lease_until=clock()+90))
                db.execute(update(runtime_workers).where(runtime_workers.c.id==self.identity).values(heartbeat=clock()))

    def run_once(self):
        with self.db.transaction() as db:
            if self.db.get(runtime_workers,self.identity,db=db):
                db.execute(update(runtime_workers).where(runtime_workers.c.id==self.identity).values(heartbeat=clock()))
            else:db.execute(runtime_workers.insert().values(id=self.identity,heartbeat=clock()))
        if clock()-self.last_schedule>30:
            self.schedule();self.last_schedule=clock()
        self.recover();job=self.claim()
        if not job: return False
        halt=threading.Event();pulse=threading.Thread(target=self.heartbeat,args=(job["id"],halt),daemon=True);pulse.start()
        status,error="done",""
        try:
            t,target=job["tenant_id"],job["target_id"]
            if job["kind"]=="task": self.ops.process_task(target,t)
            elif job["kind"]=="action": self.ops.execute(target,t)
            elif job["kind"]=="notify": self.feishu.notify(target,t)
            elif job["kind"]=="notify_result": self.feishu.notify(target,t,True)
            elif job["kind"]=="notify_task": self.feishu.notify_task(target,t)
            elif job["kind"]=="feishu_event": self.feishu.event_job(target,t)
            elif job["kind"]=="shopify_event": self.shopify_event(target,t)
            elif job["kind"]=="refresh_connection":
                action = self.db.get(actions, target, t)
                if action:
                    task = self.ops.submit({"id":"readback-worker","tenant_id":t}, "操作后同步与库存核对", "sync", {}, action["connection_id"], "refresh-"+target)
                    self.ops.process_task(task["id"], t)
            else: raise BusinessError("未知作业类型")
        except (BusinessError,PlatformError,FeishuError) as e:
            status,error="failed",str(e)[:300]
            if job["kind"]=="action":
                a=self.db.get(actions,job["target_id"],job["tenant_id"])
                if a and a["status"]=="approved":self.ops.action_failure(a,error)
        except Exception as e: status,error="failed","内部错误："+type(e).__name__
        finally:
            halt.set();pulse.join(timeout=2)
            with self.db.transaction() as db:
                db.execute(update(jobs).where(jobs.c.id==job["id"],jobs.c.worker==self.identity).values(status=status,error=error,lease_until=0))
            self.ops.refresh_commands(job["tenant_id"])
        return True

    def shopify_event(self,event_id,t):
        ev=self.db.get(events,event_id)
        conn=self.db.get(connections,ev["connection_id"],t)
        if not conn: return
        if ev["topic"] in ("app/uninstalled","shop/redact"):
            with self.db.transaction() as db:
                db.execute(update(connections).where(connections.c.id==conn["id"],connections.c.tenant_id==t).values(status="revoked",secret="",info={"redacted":True}))
                db.execute(resources.delete().where(resources.c.connection_id==conn["id"],resources.c.tenant_id==t))
                db.execute(update(actions).where(actions.c.connection_id==conn["id"],actions.c.tenant_id==t,actions.c.status.in_(["pending","approved"])).values(status="rejected",reason="店铺已卸载或请求删除"))
                self.db.audit(db,t,"shopify","shopify.revoked",conn["id"],{"topic":ev["topic"]})
            self.ops.shared.disconnect(t, conn["id"])
        elif ev["topic"] not in ("customers/data_request","customers/redact"):
            # Query current state, rather than trust webhook order or persist customer payloads.
            actor={"id":"shopify-webhook","tenant_id":t}
            self.ops.submit(actor,"店铺事件同步","sync",{},conn["id"],"hook-sync-"+conn["id"]+"-"+str(int(clock()//30)))
        else:
            with self.db.transaction() as db:
                db.execute(update(events).where(events.c.id==event_id).values(status="needs_review"))
                self.db.audit(db,t,"shopify","privacy.review_required",event_id,{"topic":ev["topic"],"order_count":len(ev["payload"].get("order_ids",[]))})
            return
        with self.db.transaction() as db: db.execute(update(events).where(events.c.id==event_id).values(status="processed",payload={}))

    def run(self):
        while not self.stop_event.is_set():
            try:
                busy=self.run_once()
            except Exception: busy=False
            if not busy: self.stop_event.wait(1)

    def schedule(self):
        for tenant in self.db.list(tenants,limit=100000):
            config=tenant["settings"]
            if not config.get("sync_enabled"):continue
            interval=config.get("sync_interval_minutes",30)*60
            if clock()-config.get("last_schedule",0)<interval:continue
            for con in self.db.list(connections,tenant["id"],100,status="active"):
                if con["provider"] not in PROVIDERS or not PROVIDERS[con["provider"]].operations:
                    continue
                self.ops.submit({"id":"scheduler","tenant_id":tenant["id"]},"定时同步店铺","sync",{},con["id"],"schedule-"+con["id"]+"-"+str(int(clock()//interval)))
            with self.db.transaction() as db:
                current=row(db.execute(select(tenants).where(tenants.c.id==tenant["id"]).with_for_update()).first())
                db.execute(update(tenants).where(tenants.c.id==tenant["id"]).values(settings=dict(current["settings"],last_schedule=clock())))

if __name__=="__main__":
    from .api import build_services
    db,vault,ops,feishu,settings=build_services()
    w=Worker(db,ops,feishu)
    try: w.run()
    except KeyboardInterrupt: w.stop()
