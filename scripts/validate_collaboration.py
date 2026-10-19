"""Real development probes; preserve every attempt, never report frozen-set accuracy."""
import hashlib
import json
import re
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
OUTPUT=ROOT/'.runtime/collaboration-probes-2.1.jsonl'

def check_review_content(content, known_secrets=()):
    assert not any(s in content for s in known_secrets if s),'Secret found in preview'
    for pattern in (r'\b[A-Za-z]:[\\/]',r'\\Users\\',r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}',r'sk-[A-Za-z0-9]{12,}'):
        assert not re.search(pattern,content,re.I),'Potential private information found; local review required'
    identifiers={os.environ.get('USERNAME',''),Path(os.environ.get('USERPROFILE','')).name}
    assert not any(re.search(r'\b'+re.escape(v)+r'\b',content,re.I) for v in identifiers if v),'Local account identifier found; stop without sending'

def local_review(client, selected, manifest, job_id=None):
    """Fail closed on non-demo jobs/documents before creating a real investigation."""
    from opslens.collector import collect
    from opslens.config import secrets
    from opslens.security import redact
    review={'checked_at':datetime.now(timezone.utc).isoformat(),'manuals':[],'runtime':{},'sample':None}
    for d in selected:
        m=next(x for x in manifest if x['source']==d['source'])
        path=ROOT/'data/knowledge'/(m['slug']+'.md')
        assert d['sha256']==hashlib.sha256(path.read_bytes()).hexdigest()
        review['manuals'].append({'source':m['source'],'sha256':d['sha256'],'content':path.read_text(encoding='utf-8')})
    for target in ('fault','healthy'):
        items=collect(target)
        assert all(i['success'] for i in items),'Local preview collection incomplete'
        diag=next(i for i in items if i['source']=='lab.diagnostics')
        for job in json.loads(diag['content']).get('database',{}).get('jobs',[]):
            response=client.get('/api/v1/lab-jobs/'+target+'/'+job['id']);response.raise_for_status()
            assert response.json()['text']=='OpsLens AI makes evidence clear','Non-demo job found; stop without sending'
        review['runtime'][target]=items
    if job_id:
        response=client.get('/api/v1/lab-jobs/fault/'+job_id);response.raise_for_status()
        review['sample']=response.json()
        assert review['sample']['text']=='OpsLens AI makes evidence clear'
    content=redact(json.dumps(review,ensure_ascii=False,default=str),secrets())
    check_review_content(content,secrets())
    name='outbound-review-2.1-'+uuid4().hex+'.json'
    (ROOT/'.runtime'/name).write_text(content,encoding='utf-8')
    print('Local review passed: fixed demo jobs, nine source-verified manuals; preview '+name,flush=True)

def run(mode='worker'):
    with httpx.Client(base_url='http://127.0.0.1:18190',trust_env=False,timeout=20) as c:
        ready=c.get('/api/v1/investigations/status');ready.raise_for_status()
        if not ready.json()['available']:raise RuntimeError(ready.json()['reason'])
        docs=c.get('/api/v1/kb/documents').json();indexes=c.get('/api/v1/kb/indexes').json()
        index=next(i for i in indexes if i['active'] and i['mode']=='semantic' and i['status']=='ready')
        selected=[d for d in docs if d['id'] in index['document_ids']]
        manifest=json.loads((ROOT/'data/knowledge/manifest.json').read_text(encoding='utf-8'))
        assert len(selected)==len(manifest)==9,'Only nine demo manuals are authorized'
        for d in selected:
            m=next(x for x in manifest if x['source']==d['source'])
            assert d['sha256']==hashlib.sha256((ROOT/'data/knowledge'/(m['slug']+'.md')).read_bytes()).hexdigest()
        job=None;target='fault'
        question='这个实例的指定任务为什么一直等待处理？请结合当前运行证据与手册调查。'
        if mode=='worker':
            case=next(x for x in c.get('/api/v1/fault-cases').json() if x['id']=='worker_stopped')
            job=case['sample_job_id']
            sample=c.get('/api/v1/lab-jobs/fault/'+job).json()
            assert sample['text']=='OpsLens AI makes evidence clear' and sample['status']=='queued'
        elif mode=='manual':
            target='healthy'
            question='只查手册：文本统计任务的字符数和词数采用什么规则？无需检查当前实例，也无需判断当前故障原因。'
        elif mode=='insufficient':
            target='unconnected';question='这个未连接实例的任务一直没有完成。请调查原因，并说明还需要哪些信息。'
        elif mode=='status':
            target='healthy';question='只查看当前实例的容器运行状态，不需要查手册，也不需要判断某个业务故障。'
        else:raise ValueError('Use worker, manual, insufficient or status')
        local_review(c,selected,manifest,job)
        body={'request_id':str(uuid4()),'target':target,'question':question,'job_id':job,'mode':'multi'}
        response=c.post('/api/v1/investigations',json=body);response.raise_for_status();task=response.json()
        print('submitted '+mode+' '+task['id'],flush=True)
        end=time.monotonic()+300
        while task['status'] in ('queued','running') and time.monotonic()<end:
            time.sleep(.7)
            response=c.get('/api/v1/investigations/'+task['id']);response.raise_for_status();task=response.json()
        checks=0;kinds=set()
        for f in (task.get('report') or {}).get('findings',[]):
            for q in f['references']:
                ref=next(r for r in task['references'] if r['id']==q['id'])
                response=c.get(ref['url']);response.raise_for_status();original=response.json()
                assert q['quote'] in (original['content'] if ref['kind']=='runtime' else original['page_text'])
                checks+=1;kinds.add(ref['kind'])
        record={'time':datetime.now(timezone.utc).isoformat(),'probe':mode,'task':task,'citation_checks':checks,'cited_kinds':sorted(kinds)}
        with OUTPUT.open('a',encoding='utf-8') as f:f.write(json.dumps(record,ensure_ascii=False,default=str)+'\n')
        print(json.dumps({'probe':mode,'id':task['id'],'status':task['status'],'report':task['report'],'collaboration':task['collaboration'],'metrics':task['metrics'],'citation_checks':checks,'kinds':sorted(kinds),'error':task['error']},ensure_ascii=False,default=str),flush=True)
        if task['status']!='completed':raise RuntimeError(task['error'] or 'task did not finish')
        if mode=='worker':
            assert task['report']['assessment']=='supported' and kinds=={'knowledge','runtime'}
            assert {t['role'] for t in task['collaboration']['tasks']}=={'runtime','knowledge'}
        elif mode=='insufficient':assert task['report']['assessment']=='insufficient' and task['report']['root_cause'] is None
        elif mode=='manual':assert {t['role'] for t in task['collaboration']['tasks']}=={'knowledge'} and kinds=={'knowledge'}
        elif mode=='status':assert {t['role'] for t in task['collaboration']['tasks']}=={'runtime'} and kinds=={'runtime'}
if __name__=='__main__':run(sys.argv[1] if len(sys.argv)>1 else 'worker')
