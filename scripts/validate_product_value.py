"""Opt-in real-model usability pilot. Immutable run directory, explicit API previews, no repairs/execution tools."""
import argparse,json,time,socket,traceback,os,hashlib,threading,subprocess
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.request import urlopen
from contextlib import ExitStack
from collections import Counter
from pathlib import Path
from datetime import datetime,timezone
from uuid import uuid4
import httpx
from psycopg import sql
from opslens import db
from opslens.diagnostic import intake
from opslens.rag.provider import QuotaGuard

API='http://127.0.0.1:18190/api/v1'

def original_fingerprints():
    result={}
    with db.connect() as c:
        for table in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename NOT LIKE 'diagnostic_%' ORDER BY tablename").fetchall():
            rows=c.execute(sql.SQL('SELECT to_jsonb(t)::text AS value FROM {} t').format(sql.Identifier(table['tablename']))).fetchall()
            result[table['tablename']]=dict(Counter(hashlib.sha256(r['value'].encode()).hexdigest() for r in rows))
    return result

def failure(fn):
    try:fn()
    except Exception:return traceback.format_exc()
    raise RuntimeError('Expected real failure was not reproduced')

def noisy(log):
    lines=[]
    for n in range(2000):
        lines.append(f'INFO fixture heartbeat tick={n} status=running')
    return '\n'.join(lines)+'\n'+log+'\n'

def fixtures(stack):
    dependency=stack.enter_context(socket.socket());dependency.bind(('127.0.0.1',0));dependency.listen()
    reservation=stack.enter_context(socket.socket());reservation.bind(('127.0.0.1',0));wrong=reservation.getsockname()
    def connect_wrong():
        with socket.socket() as client:client.settimeout(5);client.connect(wrong)
    log=failure(connect_wrong)
    if 'ConnectionRefusedError' not in log:raise RuntimeError('Not a real refusal')
    def inspect_dependency():
        with socket.create_connection(dependency.getsockname(),timeout=2):pass
        return f'当前配置 DEPENDENCY_HOST={wrong[0]} DEPENDENCY_PORT={wrong[1]}\n独立依赖实际监听={dependency.getsockname()}\n实际监听地址连接成功；配置地址连接被拒绝。'
    occupied=stack.enter_context(socket.socket())
    if hasattr(socket,'SO_EXCLUSIVEADDRUSE'):occupied.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
    occupied.bind(('127.0.0.1',0));occupied.listen();port=occupied.getsockname()[1]
    def bind_again():
        with socket.socket() as service:service.bind(('127.0.0.1',port))
    collision=failure(bind_again)
    def inspect_port():
        with socket.socket() as alternate:alternate.bind(('127.0.0.1',0));free=alternate.getsockname()[1]
        return f'隔离测试监听进程PID={os.getpid()}，监听地址127.0.0.1:{port}，第二实例也配置同端口{port}。另一个空闲端口{free}实际绑定成功。未停止已有服务。'
    configuration={'SERVICE_PORT':'invalid-port'}
    def configured_start():
        port_value=int(configuration['SERVICE_PORT'])
        with socket.socket() as service:service.bind(('127.0.0.1',port_value))
    invalid=failure(configured_start)
    def inspect_config():
        original=dict(configuration);configuration['SERVICE_PORT']='0';configured_start()
        return f'脱敏配置 SERVICE_PORT原值={original["SERVICE_PORT"]}，类型str；启动代码将其传给int。仅在独立测试夹具改成合法整数0后，实际端口绑定成功。'
    server_errors=[]
    config={'sample_count':'many'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            try:
                count=int(config['sample_count'])
                self.send_response(200);self.end_headers();self.wfile.write(str(count).encode())
            except Exception:
                server_errors.append(traceback.format_exc());self.send_response(500);self.end_headers();self.wfile.write(b'internal server error')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    stack.callback(server.server_close);stack.callback(server.shutdown)
    url='http://127.0.0.1:'+str(server.server_address[1])+'/metrics'
    def endpoint():
        with urlopen(url,timeout=2) as r:return r.status,r.read().decode()
    request_error=failure(endpoint)
    def inspect_api():
        diagnostic=server_errors[-1]
        config['sample_count']='3';status,body=endpoint()
        return '实际服务端异常：\n'+diagnostic+f'\n检查配置sample_count原值many；仅在隔离测试修改为数字3后，实际GET /metrics返回{status}，正文{body}。'
    return [
      {'id':'api_500_configuration','problem':'Python后端GET /metrics返回500，附件是客户端日志，请给我下一步检查。','environment':'Windows，Python本机HTTP服务；独立复现，非业务数据。','log':noisy(request_error),'truth':'服务端配置sample_count非数字导致int解析失败与HTTP500','check':inspect_api},
      {'id':'dependency_wrong_port','problem':'Python后端请求依赖时失败，请先告诉我最值得做的检查。','environment':'Windows，本机隔离Python套接字服务；仅供试用，不连接实际业务。','log':noisy(log),'truth':'依赖服务运行正常，调用方配置了未监听端口','check':inspect_dependency},
      {'id':'startup_port_conflict','problem':'本机启动另一个后端实例时报错，请帮助排查。','environment':'Windows，本机Python服务，无Docker。','log':noisy(collision),'truth':'两个实例配置同一已监听端口','check':inspect_port},
      {'id':'startup_invalid_port','problem':'后端进程刚启动就退出，下面是启动日志。','environment':'Windows，Python 3.14，本机服务。','log':noisy(invalid),'truth':'SERVICE_PORT值不是合法整数','check':inspect_config},
      {'id':'insufficient_500','problem':'接口返回500，其他资料暂时没有。','environment':'','log':'INFO received request /health\nINFO request complete\n','truth':'资料不足，不能确认根因','check':None},
    ]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--confirm-send-fixture-only',action='store_true');parser.add_argument('--skip-case',action='append',default=[])
    parser.add_argument('--case',action='append',default=[],choices=['api_500_configuration','dependency_wrong_port','startup_port_conflict','startup_invalid_port','insufficient_500'])
    parser.add_argument('--keep-originals',action='store_true',help='Keep explicitly selected first-round logs in follow-up previews')
    parser.add_argument('--rounds',type=int,choices=(1,2),default=2)
    parser.add_argument('--request-cap',type=int,default=12,choices=range(1,13));args=parser.parse_args()
    if not args.confirm_send_fixture_only:raise SystemExit('Explicit fixture sending confirmation required')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');root=Path('.runtime')/('value-validation-'+stamp);root.mkdir()
    state={'started_at':datetime.now(timezone.utc).isoformat(),'source_revision':subprocess.check_output(['git','-c','safe.directory='+Path.cwd().as_posix(),'rev-parse','HEAD'],text=True).strip(),'source':'real isolated faults and generated fixture heartbeat logs','authorization':'user requested usage validation; exact sanitized fixture previews confirmed by executor','cases':[]}
    def save(): (root/'results.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    baseline=original_fingerprints();(root/'old-row-fingerprints.json').write_text(json.dumps(baseline),encoding='utf-8')
    client=httpx.Client(base_url=API,timeout=10,trust_env=False)
    def api(path,body=None):
        response=client.get(path) if body is None else client.post(path,json=body)
        response.raise_for_status();return response.json()
    previous=api('/diagnostic/settings')['settings'];state['original_settings']=previous;save()
    # Default profile stays on the existing Qwen model. Never choose another model or bypass quota checks.
    if previous['mode']!='free' or previous['model']!='qwen3.8-max':raise RuntimeError('Pilot requires the existing free Qwen profile; no automatic switch')
    profile={**previous,'enabled':True,'daily_limit':min(20,previous['daily_limit']),'max_output':2048}
    status=None
    try:
      status=api('/diagnostic/settings',profile)
      if not status['available']:
        state['blocked']=status['reason'];save();raise RuntimeError(status['reason'])
      state['skipped_cases']=args.skip_case;state['selected_cases']=args.case;state['keep_originals']=args.keep_originals;state['request_cap']=args.request_cap;state['model']=profile['model'];state['profile_version']=status['version'];save()
      # Both stage calls count. The daily database limit enforces this bound even if a worker needs correction.
      if 20-status['used_today']>args.request_cap:
          profile={**profile,'daily_limit':status['used_today']+args.request_cap}
          status=api('/diagnostic/settings',profile);state['profile_version']=status['version'];save()
      with ExitStack() as stack:
       for case in fixtures(stack):
        if args.case and case['id'] not in args.case:continue
        if case['id'] in args.skip_case:continue
        row={k:v for k,v in case.items() if k not in ('log','check')};row['rounds']=[];state['cases'].append(row);save()
        sid=api('/diagnostic/sessions',{'request_id':str(uuid4()),'problem':case['problem'],'project':'0.5真实模型小样本试用','environment':case['environment']})['id'];row['session_id']=sid
        s=api(f'/diagnostic/sessions/{sid}/sources',{'request_id':str(uuid4()),'name':case['id']+'.log','kind':'log','content':case['log']});source_ids=[s['id']]
        for round_index in range(args.rounds if case['check'] else 1):
         if round_index:
            output=case['check']();row['actual_check_output']=output
            extra=api(f'/diagnostic/sessions/{sid}/sources',{'request_id':str(uuid4()),'name':'隔离夹具检查输出','kind':'check_result','content':output});source_ids=([s['id']] if args.keep_originals else [])+[extra['id']]
         preview=api(f'/diagnostic/sessions/{sid}/previews',{'request_id':str(uuid4()),'source_ids':source_ids})
         # This preview contains only explicitly created fixtures; secrets/path/PII are sanitized locally.
         text=json.dumps(preview,ensure_ascii=False)
         if '\\Users\\' in text or any(secret in text for secret in [os.environ.get('DASHSCOPE_APIKEY','')] if secret):raise RuntimeError('Private content in fixture preview')
         (root/(case['id']+f'-preview-{round_index+1}.json')).write_text(text,encoding='utf-8')
         record={'preview_id':preview['id'],'coverage':preview['coverage'],'started_at':datetime.now(timezone.utc).isoformat()};row['rounds'].append(record);save()
         start=time.monotonic()
         t=api(f'/diagnostic/sessions/{sid}/turns',{'request_id':str(uuid4()),'preview_id':preview['id'],'digest':preview['digest'],'confirm':True});record['turn_id']=t['id'];save()
         while t['status'] in ('queued','running'):
            if time.monotonic()-start>240:raise RuntimeError('Result unknown; do not resend the issued request')
            time.sleep(1);t=api('/diagnostic/turns/'+t['id'])
         record.update(elapsed_seconds=round(time.monotonic()-start,2),status=t['status'],report=t.get('report'),metrics=t.get('metrics'),error=t.get('error'));save()
         print(json.dumps({'case':case['id'],'round':round_index+1,'status':t['status'],'elapsed_seconds':record['elapsed_seconds'],'summary':(t.get('report') or {}).get('summary'),'error':t.get('error')},ensure_ascii=False),flush=True)
         if t['status']!='completed':state['stopped_on_failure']=True;save();return
        if case['check'] and args.rounds==1:
            row['actual_check_output']=case['check']();row['check_recorded_at']=datetime.now(timezone.utc).isoformat();row['followup_pending']=True;save()
        exported=client.get(f'/diagnostic/sessions/{sid}/export');exported.raise_for_status();(root/(case['id']+'.md')).write_text(exported.text,encoding='utf-8')
    finally:
      current=api('/diagnostic/settings')
      if current['settings']==profile:
          api('/diagnostic/settings',previous);state['settings_restored']=True
      else:
          state['settings_restored']=current['settings']==previous
          state['settings_restore_note']='Different settings detected; preserved the current user configuration'
      state['finished_at']=datetime.now(timezone.utc).isoformat()
      with db.connect() as c:
        ids=[r['turn_id'] for case in state['cases'] for r in case['rounds'] if r.get('turn_id')]
        req=c.execute('SELECT id,turn_id,stage,status,usage,error,created_at,finished_at FROM diagnostic_requests WHERE turn_id=ANY(%s::uuid[]) ORDER BY created_at',(ids,)).fetchall()
      state['requests']=json.loads(json.dumps(req,default=str));after=original_fingerprints()
      state['original_rows_preserved']=all(all(after.get(t,{}).get(h,0)>=n for h,n in rows.items()) for t,rows in baseline.items());save()
      print('Evidence directory: '+str(root),flush=True)
      client.close()

if __name__=='__main__':main()
