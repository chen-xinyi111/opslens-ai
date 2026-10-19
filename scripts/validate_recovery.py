"""Authorized isolated development recovery; clearly labelled test approvals."""
import json,sys,time
from pathlib import Path
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from opslens import db
from opslens.collector import containers,docker
from opslens.recovery import store
from opslens.recovery.contracts import Approve
from opslens.recovery.adapter import Adapter
from opslens.security import redact
from opslens.config import secrets

INV='50737d9a-fb15-437e-be9a-90413b1cdee1'
def isolation():
    value={}
    for project in ('opslens-lab-healthy','agent-commerce-stage3'):
        ids=docker(['ps','-aq','--filter','label=com.docker.compose.project='+project]).split()
        for cid in ids:
            obj=json.loads(docker(['inspect',cid]))[0]
            value[cid]={'state':obj['State']['Status'],'started_at':obj['State']['StartedAt'],'image':obj['Image']}
    return value
def run():
    baseline=isolation();adapter=Adapter();before=adapter.snapshot();assert before['bindings']['worker']['state']=='exited'
    records=[]
    with httpx.Client(base_url='http://127.0.0.1:18190',trust_env=False,timeout=15) as c:
        inv=c.get('/api/v1/investigations/'+INV).json();assert inv['verification']['status']=='confirmed'
        body={'request_id':str(uuid4()),'investigation_id':INV,'action':'start_worker'}
        p=c.post('/api/v1/recovery/plans',json=body);p.raise_for_status();p=p.json()
        assert adapter.snapshot()['bindings']['worker']['state']=='exited'
        q=c.post('/api/v1/recovery/plans',json=body|{'request_id':str(uuid4())});q.raise_for_status();q=q.json()
        bad=c.post('/api/v1/recovery/plans/'+p['id']+'/approve',json={'request_id':str(uuid4()),'version':p['version'],'digest':p['digest'],'confirm':True})
        assert bad.status_code==400
        assert adapter.snapshot()['bindings']['worker']['state']=='exited'
        a=Approve(request_id=uuid4(),version=q['version'],digest=q['digest'],confirm=True)
        store.approve(q['id'],a,actor='development_selfcheck')
        end=time.monotonic()+130
        while time.monotonic()<end:
            task=c.get('/api/v1/recovery/plans/'+q['id']).json()
            if task['status'] not in ('approved','executing','verifying'):break
            time.sleep(.5)
        assert task['status']=='completed',task
        assert task['result']['probe']['result']=={'char_count':31,'word_count':5}
        store.approve(q['id'],a,actor='development_selfcheck')
        with db.connect() as conn:assert conn.execute('SELECT count(*) n FROM recovery_actions WHERE plan_id=%s',(q['id'],)).fetchone()['n']==1
        events=c.get('/api/v1/recovery/plans/'+q['id']+'/events').text
        assert all(v in events for v in ('approved','action_issued','probe_submitted','finished'))
        records.append({'plan':task,'unapproved_worker_unchanged':True,'old_version_rejected':True,'duplicate_action_count':1})
    assert baseline==isolation(),'Normal lab or project one changed'
    path=ROOT/'.runtime'/('recovery-probes-2.3-'+uuid4().hex+'.json')
    path.write_text(redact(json.dumps({'records':records,'other_projects_unchanged':True,'model_calls':0},ensure_ascii=False,indent=2),secrets()),encoding='utf-8')
    print('Real approved worker recovery, business probe, old approval rejection and repeat protection passed. '+path.name,flush=True)
if __name__=='__main__':run()
