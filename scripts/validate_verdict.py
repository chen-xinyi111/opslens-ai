"""Controlled-candidate checks against real sources, outside production history.

These check the verifier component, not automatic root-cause accuracy. Every
candidate is explicitly test input and every observed service/job is real.
"""
import json
import sys
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4
import httpx
from validate_collaboration import local_review,check_review_content
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from opslens.investigation.verification import VERIFIER_PROMPT,compact_context,validate_verdict
from opslens.investigation.engine import validate_report
from opslens.investigation.model import GenerationProvider
from opslens.investigation.tools import Tools
from opslens.config import secrets

def run():
    with httpx.Client(base_url='http://127.0.0.1:18190',trust_env=False,timeout=20) as c:
        indexes=c.get('/api/v1/kb/indexes').json()
        index=next(i for i in indexes if i['active'] and i['mode']=='semantic' and i['status']=='ready')
        selected=[d for d in c.get('/api/v1/kb/documents').json() if d['id'] in index['document_ids']]
        manifest=json.loads((ROOT/'data/knowledge/manifest.json').read_text(encoding='utf-8'));assert len(selected)==len(manifest)==9
        job=next(v for v in c.get('/api/v1/fault-cases').json() if v['id']=='worker_stopped')['sample_job_id']
        local_review(c,selected,manifest,job)
        tools=Tools({'target':'fault','job_id':job,'index_id':index['id']},namespace='component-')
        runtime=tools.execute('collect_runtime',{})['references']
        services=next(r for r in runtime if r.get('source')=='docker.services')
        task=tools.execute('read_job',{'job_id':job})['reference']
        states=json.loads(services['content']);assert any(v['service']=='worker' and v['state']=='exited' for v in states)
        assert any(v['service']=='redis' and v['state']=='running' for v in states)
        assert json.loads(task['content'])['status']=='queued'
        refs=[services,task]
        check_review_content(json.dumps(refs,ensure_ascii=False,default=str),secrets())
        run_id=uuid4().hex
        provider=GenerationProvider();provider.prepare_parallel()
        for name,root,statement,quote in (
            ('supported_input','worker 已退出，没有后台消费者处理已入队的任务。','worker 已退出。','"service": "worker",\n    "state": "exited"'),
            ('false_input','Redis 已停止，导致该任务一直等待。','Redis 已停止。','"service": "redis",\n    "state": "running"'),
        ):
            candidate={'assessment':'supported','summary':'待核验的人工测试候选','root_cause':root,
                'findings':[{'statement':statement,'references':[{'id':services['id'],'quote':quote}]},
                    {'statement':'指定任务仍queued。','references':[{'id':task['id'],'quote':'"status": "queued"'}]}],
                'missing_information':[],'next_steps':[]}
            candidate=validate_report(candidate,refs,'fault').model_dump()
            context={'input':{'target':'fault','job_id':job,'question':'请核验候选原因是否符合实际运行证据。'},'candidate':candidate,'references':refs,'detected_conflicts':[]}
            messages=[{'role':'system','content':VERIFIER_PROMPT},{'role':'user','content':json.dumps(compact_context(context),ensure_ascii=False,default=str)}]
            check_review_content(json.dumps(messages,ensure_ascii=False),secrets())
            response=provider.generate(messages,[])
            content=response['content'].strip()
            if content.startswith('```'):content=content.split('\n',1)[1].rsplit('```',1)[0].strip()
            record={'time':datetime.now(timezone.utc).isoformat(),'kind':'controlled candidate real verifier component, not an automatic investigation or accuracy result',
                'test':name,'candidate':candidate,'references':refs,'response':response}
            path=ROOT/'.runtime'/('verifier-component-2.2-'+name+'-'+run_id+'.json')
            path.write_text(json.dumps(record,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
            verdict=validate_verdict(json.loads(content),refs,'fault',candidate)
            if name=='supported_input':assert verdict.decision=='confirmed'
            else:assert verdict.decision=='rejected' and verdict.report.root_cause is None
            for f in verdict.report.findings:
                for q in f.references:
                    ref=next(r for r in refs if r['id']==q.id)
                    original=c.get(ref['url']);original.raise_for_status();assert q.quote in original.json()['content']
            record['verdict']=verdict.model_dump();path.write_text(json.dumps(record,ensure_ascii=False,default=str,indent=2),encoding='utf-8')
            print(json.dumps({'test':name,'decision':verdict.decision,'reason':verdict.reason,'usage':response.get('usage')},ensure_ascii=False),flush=True)
if __name__=='__main__':run()
