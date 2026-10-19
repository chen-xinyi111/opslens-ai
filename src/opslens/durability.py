"""PostgreSQL leases and replayable external-operation checkpoints.

Graphs reconstruct pure state from saved responses; external effects are never
replayed. Operation keys include a stable role lane and occurrence number.
"""
import contextvars
import hashlib
import json
import threading
import time
from contextlib import contextmanager
from opslens import db
from opslens.rag.store import jsonb
from opslens.rag.provider import ModelBlocked

ACTIVE = contextvars.ContextVar('opslens_runtime', default=None)
INTERNAL = contextvars.ContextVar('opslens_runtime_internal', default=False)
CURRENT_OPERATION = contextvars.ContextVar('opslens_current_operation',default=None)
CURRENT_STAGE = contextvars.ContextVar('opslens_current_stage',default=None)
FAMILIES = {
 'diagnostic': ('diagnostic_turns','diagnostic_events','turn_id','queued','running',240),
 'collection': ('collections','collection_events','collection_id','queued','running',240),
 'knowledge': ('kb_tasks','kb_events','task_id','queued','running',600),
 'investigation': ('investigations','investigation_events','investigation_id','queued','running',240),
 'recovery': ('recovery_plans','recovery_events','plan_id','approved','executing',150),
 'evaluation': ('evaluation_items','evaluation_events','item_id','pending','running',240),
}
class LeaseLost(Exception): pass
class UnknownResult(ModelBlocked): pass
class TaskTimeout(TimeoutError): pass

def canonical(value):
    if isinstance(value,dict):
        result={k:canonical(v) for k,v in sorted(value.items())}
        if isinstance(result.get('content'),str):
            try:result['content']=canonical(json.loads(result['content']))
            except (ValueError,TypeError):pass
        return result
    if isinstance(value,list):
        values=[canonical(v) for v in value]
        if values and all(isinstance(v,dict) and 'id' in v for v in values):
            values.sort(key=lambda v:str(v['id']))
        return values
    return value

def event(c,family,i,kind,payload):
    _,table,fk,*_=FAMILIES[family]
    c.execute(f'INSERT INTO {table}({fk},kind,payload) VALUES (%s,%s,%s)',(i,kind,jsonb(payload)))

def claim(family,task_id=None):
    table,_,_,queued,running,_=FAMILIES[family]
    with db.connect() as c:
        # Serialize claims across process instances, including the single recovery target.
        c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,3101))',(family,))
        while True:
            active="t.status IN ('executing','verifying')" if family=='recovery' else "t.status='running'"
            row=c.execute(f"SELECT t.*,d.attempt AS old_attempt FROM {table} t LEFT JOIN durable_tasks d ON d.family=%s AND d.task_id=t.id WHERE (t.status=%s OR ({active} AND d.lease_until<now()))"+(" AND t.id=%s" if task_id else "")+" ORDER BY t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1",(family,queued,*([task_id] if task_id else []))).fetchone()
            if not row:return None
            if family=='recovery' and c.execute("SELECT 1 FROM recovery_plans t JOIN durable_tasks d ON d.family='recovery' AND d.task_id=t.id WHERE t.status IN ('executing','verifying') AND d.lease_until>=now() AND t.id<>%s",(row['id'],)).fetchone():return None
            old=row.pop('old_attempt') or 0
            if old>=3:
                status='manual' if family=='recovery' else 'failed'
                reason='已达到两次自动接管上限，已有记录保留，请人工确认'
                c.execute(f'UPDATE {table} SET status=%s,error=%s,finished_at=now() WHERE id=%s',(status,reason,row['id']))
                if family=='knowledge' and row['kind']=='index':
                    c.execute("UPDATE kb_indexes SET status='failed',error=%s,finished_at=now() WHERE id=%s AND status='building'",(reason,row['input']['index_id']))
                c.execute('UPDATE durable_tasks SET stop_reason=%s WHERE family=%s AND task_id=%s',(reason,family,row['id']))
                event(c,family,row['id'],'finished',{'status':status,'message':reason})
                continue
            attempt=old+1
            if old:
                event(c,family,row['id'],'interrupted',{'message':'上次处理已中断，保存步骤和结果保留'})
                event(c,family,row['id'],'takeover',{'message':'失效租约已接管，正在恢复保存步骤','attempt':attempt})
            c.execute("INSERT INTO durable_tasks(family,task_id,attempt,lease_until,heartbeat_at) VALUES (%s,%s,%s,now()+interval '30 seconds',now()) ON CONFLICT(family,task_id) DO UPDATE SET attempt=excluded.attempt,lease_until=excluded.lease_until,heartbeat_at=now()",(family,row['id'],attempt))
            row=c.execute(f'UPDATE {table} SET status=%s WHERE id=%s RETURNING *',(running if row['status']==queued else row['status'],row['id'])).fetchone()
            event(c,family,row['id'],'resumed' if old else 'running',{'message':'已从保存位置继续；已完成调用直接复用' if old else '任务正在处理','recovery_count':old})
            row['_attempt']=attempt
            return row

def describe(c,family,i):
    row=c.execute('SELECT attempt,checkpoint,elapsed_seconds,stop_reason FROM durable_tasks WHERE family=%s AND task_id=%s',(family,i)).fetchone()
    value={'recovery_count':max(0,row['attempt']-1),'saved_step':row['checkpoint'].get('step'),'elapsed_seconds':row['elapsed_seconds'],'stop_reason':row['stop_reason']} if row else {'recovery_count':0,'saved_step':None,'stop_reason':None}
    if family=='recovery':value['action_count']=c.execute('SELECT count(*) n FROM recovery_actions WHERE plan_id=%s',(i,)).fetchone()['n']
    return value

class Runtime:
    def __init__(self,family,task):
        self.family=family;self.id=task['id'];self.attempt=task['_attempt']
        self.started=time.monotonic();self.tick=self.started;self.lock=threading.RLock();self.counts={};self.stop=threading.Event();self.elapsed=0
        with db.connect() as c:
            row=c.execute('SELECT elapsed_seconds FROM durable_tasks WHERE family=%s AND task_id=%s',(family,self.id)).fetchone()
            self.elapsed=row['elapsed_seconds']
    def guard(self,c,deadline=True,lock=True):
        row=c.execute('SELECT * FROM durable_tasks WHERE family=%s AND task_id=%s'+(' FOR UPDATE' if lock else ''),(self.family,self.id)).fetchone()
        from datetime import datetime,timezone
        if not row or row['attempt']!=self.attempt or not row['lease_until'] or row['lease_until']<=datetime.now(timezone.utc):raise LeaseLost('执行批次已失效，禁止旧线程写入')
        task=c.execute(f"SELECT status FROM {FAMILIES[self.family][0]} WHERE id=%s",(self.id,)).fetchone()
        active=('executing','verifying') if self.family=='recovery' else ('running',)
        if not task or task['status'] not in active:raise LeaseLost('任务已结束，禁止再次处理')
        if deadline and self.elapsed+time.monotonic()-self.started>FAMILIES[self.family][5]:raise TaskTimeout('处理时间已达到上限，已有记录保留')
    @contextmanager
    def transaction(self,deadline=True):
        token=INTERNAL.set(True)
        try:
            with db.connect() as c:
                self.guard(c,deadline);yield c
        finally:INTERNAL.reset(token)
    @contextmanager
    def bound(self):
        token=ACTIVE.set(self)
        try:yield
        finally:ACTIVE.reset(token)
    def heartbeat(self):
        with self.lock:
            now=time.monotonic();delta=now-self.tick
            with self.transaction(deadline=False) as c:
                c.execute("UPDATE durable_tasks SET lease_until=now()+interval '30 seconds',heartbeat_at=now(),elapsed_seconds=elapsed_seconds+%s WHERE family=%s AND task_id=%s",(delta,self.family,self.id))
            self.tick=now
    @contextmanager
    def live(self):
        def pulse():
            while not self.stop.wait(5):
                try:self.heartbeat()
                except Exception:return
        thread=threading.Thread(target=pulse,daemon=True);thread.start()
        try:
            with self.bound():yield self
        finally:
            self.stop.set();thread.join(timeout=1)
            try:self.heartbeat()
            except Exception:pass
    def key(self,kind,value):
        raw=json.dumps(canonical(value),ensure_ascii=False,sort_keys=True,default=str)
        digest=hashlib.sha256(raw.encode()).hexdigest()
        with self.lock:
            n=self.counts.get((kind,digest),0);self.counts[(kind,digest)]=n+1
        return f'{kind}:{digest}:{n}'
    def operation(self,key,fn,paid=False):
        with self.transaction() as c:
            row=c.execute('SELECT * FROM durable_operations WHERE family=%s AND task_id=%s AND operation_key=%s',(self.family,self.id,key)).fetchone()
            if row and row['status']=='completed':return row['result']
            if row and row['status']=='issued' and row['paid']:raise UnknownResult('模型或检索调用已发出但结果未保存；禁止自动再次调用，请人工确认')
            c.execute("INSERT INTO durable_operations(family,task_id,operation_key,status,paid) VALUES (%s,%s,%s,'issued',%s) ON CONFLICT DO NOTHING",(self.family,self.id,key,paid))
            c.execute('UPDATE durable_tasks SET checkpoint=%s WHERE family=%s AND task_id=%s',(jsonb({'step':key,'state':'issued'}),self.family,self.id))
        result=fn()
        # Normalize datetimes identically on initial execution and replay.
        result=json.loads(json.dumps(result,ensure_ascii=False,default=str))
        with self.transaction() as c:
            c.execute("UPDATE durable_operations SET status='completed',result=%s,finished_at=now() WHERE family=%s AND task_id=%s AND operation_key=%s",(jsonb(result),self.family,self.id,key))
            c.execute('UPDATE durable_tasks SET checkpoint=%s WHERE family=%s AND task_id=%s',(jsonb({'step':key,'state':'completed'}),self.family,self.id))
            event(c,self.family,self.id,'checkpoint',{'message':'处理步骤已保存','step':key.split(':')[0]})
        return result
    def stage(self,key,fn,snapshot,restore):
        def run():
            result=fn()
            return {'output':result,'snapshot':snapshot()}
        saved=self.operation(key,run)
        restore(saved['snapshot'])
        return saved['output']
    def record(self,kind,payload,fn):
        with self.transaction() as c:fn(self.id,kind,payload,connection=c)
    def finish(self,status,**values):
        table=FAMILIES[self.family][0]
        with self.lock,self.transaction(deadline=False) as c:
            assignments=['status=%s','finished_at=now()']+[k+'=%s' for k in values]
            c.execute(f"UPDATE {table} SET {','.join(assignments)} WHERE id=%s",(status,*[jsonb(v) if isinstance(v,(dict,list)) else v for v in values.values()],self.id))
            if values.get('error'):c.execute('UPDATE durable_tasks SET stop_reason=%s WHERE family=%s AND task_id=%s',(values['error'],self.family,self.id))
            now=time.monotonic()
            c.execute('UPDATE durable_tasks SET elapsed_seconds=elapsed_seconds+%s WHERE family=%s AND task_id=%s',(now-self.tick,self.family,self.id))
            self.tick=now
            event(c,self.family,self.id,'finished',{'status':status,'message':values.get('error') or '结果已保存'})

class SavedModel:
    def __init__(self,model,runtime):self.model=model;self.runtime=runtime
    def generate(self,messages,definitions):
        key=self.runtime.key('model',[CURRENT_STAGE.get(),messages,definitions])
        with self.runtime.bound():return self.runtime.operation(key,lambda:self.model.generate(messages,definitions),paid=True)
    def prepare_parallel(self):return self.runtime.operation('connection:ready',self.model.prepare_parallel)

class SavedTools:
    def __init__(self,tools,runtime,lane='root'):
        self.tools=tools;self.runtime=runtime;self.lane=lane
    @property
    def refs(self):return self.tools.refs
    def fork(self,i,role):return SavedTools(self.tools.fork(i,role),self.runtime,i)
    def execute(self,name,args):
        key=self.runtime.key('tool',[CURRENT_STAGE.get(),self.lane,name,args])
        def run():
            with self.runtime.bound():
                token=CURRENT_OPERATION.set(key)
                try:
                    value=self.tools.execute(name,args)
                    return {'value':value,'refs':self.tools.refs,'collection_id':str(getattr(self.tools,'collection_id',None)) if getattr(self.tools,'collection_id',None) else None,'collected':getattr(self.tools,'collected',False)}
                finally:CURRENT_OPERATION.reset(token)
        # Paid requests inside knowledge search have their own finer checkpoints.
        saved=self.runtime.operation(key,run)
        self.tools.refs[:]=saved['refs']
        if hasattr(self.tools,'collection_id'):self.tools.collection_id=saved['collection_id'];self.tools.collected=saved['collected']
        return saved['value']
    def load_snapshot(self,i):
        key=self.runtime.key('snapshot',str(i))
        def run():return {'value':self.tools.load_snapshot(i),'refs':self.tools.refs}
        saved=self.runtime.operation(key,run);self.tools.refs[:]=saved['refs'];return saved['value']

@contextmanager
def write():
    rt=ACTIVE.get()
    with rt.transaction() if rt else db.connect() as c:yield c

class SavedProvider:
    def __init__(self,provider,rt):self.provider=provider;self.rt=rt
    def embed(self,texts,text_type='document'):
        key=self.rt.key('embedding',[CURRENT_OPERATION.get(),text_type,texts])
        return self.rt.operation(key,lambda:self.provider.embed(texts,text_type=text_type),paid=True)
    def rerank(self,query,texts):
        key=self.rt.key('rerank',[CURRENT_OPERATION.get(),query,texts])
        return self.rt.operation(key,lambda:self.provider.rerank(query,texts),paid=True)

def saved_provider(provider):
    rt=ACTIVE.get()
    return SavedProvider(provider,rt) if rt and not isinstance(provider,SavedProvider) else provider

def graph_node(name,fn,tools,snapshot=lambda:{},restore=lambda value:None):
    rt=ACTIVE.get()
    obj=tools
    while rt is None and obj is not None:
        rt=getattr(obj,'runtime',None);obj=getattr(obj,'child',None)
    if rt is None:return fn
    def node(state):
        key=rt.key('node',[name,state])
        def take():return {'state':snapshot(),'refs':tools.refs}
        def put(value):
            tools.refs[:]=value['refs'];restore(value['state'])
        token=CURRENT_STAGE.set(key)
        try:
            with rt.bound():return rt.stage(key,lambda:fn(state),take,put)
        finally:CURRENT_STAGE.reset(token)
    return node

def agent_scope(name,fn,tools):
    obj=tools;rt=ACTIVE.get()
    while rt is None and obj is not None:
        rt=getattr(obj,'runtime',None);obj=getattr(obj,'child',None)
    if not rt:return fn
    def node(state):
        token=CURRENT_STAGE.set(name+':'+str(state['model_calls'])+':'+str(state['tool_calls']))
        try:
            with rt.bound():return fn(state)
        finally:CURRENT_STAGE.reset(token)
    return node
