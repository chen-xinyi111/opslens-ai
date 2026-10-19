"""Development-only real checks, preserving failures and reviewing demo inputs.

Never inject a simulated report into production history or call these accuracy
measurements. Model output and exact-source checks are retained locally.
"""
import hashlib
import json
import sys
import time
from datetime import datetime,timezone
from pathlib import Path
from uuid import uuid4
import httpx
from validate_collaboration import local_review,check_review_content
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

def run(mode='conflict'):
    with httpx.Client(base_url='http://127.0.0.1:18190',trust_env=False,timeout=20) as c:
        ready=c.get('/api/v1/investigations/status');ready.raise_for_status()
        assert ready.json()['available'],ready.json()['reason']
        index=next(i for i in c.get('/api/v1/kb/indexes').json() if i['active'] and i['mode']=='semantic' and i['status']=='ready')
        selected=[d for d in c.get('/api/v1/kb/documents').json() if d['id'] in index['document_ids']]
        manifest=json.loads((ROOT/'data/knowledge/manifest.json').read_text(encoding='utf-8'))
        assert len(selected)==len(manifest)==9
        snapshot=None;job=None;target='fault'
        if mode=='conflict':
            example=c.get('/api/v1/investigations/examples/conflict').json();assert example['available'],example['reason']
            snapshot=example['snapshot_id'];job=example['job_id'];question=example['question']
            old=c.get('/api/v1/evidence/'+snapshot).json()
            assert old['success'] and old['lab']=='fault' and old['source']=='docker.services'
            assert old['sha256']==hashlib.sha256(old['content'].encode()).hexdigest()
            values=json.loads(old['content'])
            assert all(v['service'] in ('api','worker','blocker','redis','postgres') for v in values)
            assert any(v['service']=='worker' and v['state']=='running' for v in values)
            from opslens.config import secrets
            check_review_content(json.dumps(old,ensure_ascii=False),secrets())
            (ROOT/'.runtime'/('outbound-history-review-2.2-'+uuid4().hex+'.json')).write_text(json.dumps(old,ensure_ascii=False),encoding='utf-8')
        elif mode=='worker':
            job=next(v for v in c.get('/api/v1/fault-cases').json() if v['id']=='worker_stopped')['sample_job_id']
            question='这个实例的指定任务为什么一直等待处理？请结合当前运行证据与手册调查。'
        elif mode=='missing':
            target='unconnected';question='这个未连接实例的任务一直没有完成。请调查原因，并说明还需要哪些信息。'
        else:raise ValueError('Use conflict, worker or missing')
        local_review(c,selected,manifest,job)
        response=c.post('/api/v1/investigations',json={'request_id':str(uuid4()),'target':target,'question':question,'job_id':job,'mode':'verified','snapshot_id':snapshot})
        response.raise_for_status();task=response.json();print('submitted '+mode+' '+task['id'],flush=True)
        end=time.monotonic()+300
        while task['status'] in ('queued','running') and time.monotonic()<end:
            time.sleep(.7);response=c.get('/api/v1/investigations/'+task['id']);response.raise_for_status();task=response.json()
        quotes=[]
        for f in (task.get('report') or {}).get('findings',[]):quotes.extend(f['references'])
        for round in task['verification']['rounds']:
            if round.get('verdict'):
                for issue in round['verdict']['issues']:quotes.extend(issue['references'])
                for f in round['verdict']['report']['findings']:quotes.extend(f['references'])
        for issue in task['verification']['conflicts']:quotes.extend(issue['references'])
        for q in quotes:
            ref=next(r for r in task['references'] if r['id']==q['id'])
            original=c.get(ref['url']);original.raise_for_status();v=original.json()
            assert q['quote'] in (v['content'] if ref['kind']=='runtime' else v['page_text'])
        record={'time':datetime.now(timezone.utc).isoformat(),'probe':mode,'task':task,'citation_checks':len(quotes)}
        with (ROOT/'.runtime/verification-probes-2.2.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
        print(json.dumps({'id':task['id'],'status':task['status'],'report':task['report'],'verification':task['verification'],'metrics':task['metrics'],'citation_checks':len(quotes),'error':task['error']},ensure_ascii=False),flush=True)
        assert task['status']=='completed',task['error']
        assert task['metrics']['model_calls']<=8 and task['metrics']['tool_calls']<=12
        assert len(task['collaboration']['tasks'])<=6 and len(task['verification']['replans'])<=2
        if mode=='conflict':
            assert task['verification']['rounds'][0]['verdict']['decision']!='confirmed'
            assert task['verification']['replans'] and any(i['kind']=='stale_snapshot' for i in task['verification']['conflicts'])
            assert any(r['scope']=='historical' for r in task['references'])
        elif mode=='worker':assert task['verification']['status']=='confirmed'
        elif mode=='missing':assert task['report']['assessment']=='insufficient' and task['report']['root_cause'] is None and task['report']['missing_information']
if __name__=='__main__':run(sys.argv[1] if len(sys.argv)>1 else 'conflict')
