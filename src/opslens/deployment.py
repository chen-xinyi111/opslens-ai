"""Read-only local startup validation; never starts or stops services."""
import json, os, shutil, socket, subprocess, sys
from pathlib import Path
from opslens.config import ROOT
from opslens.collector import docker
GiB=1024**3
SCOPES={'Platform':(18190,18193),'Fault':(18190,18182,18193),'All':(18190,18181,18182,18193)}

def resource_checks(os_type,docker_memory,available_memory,disk_free,python_version):
    return [{'name':name,'ok':ok,'message':message} for name,ok,message in [
        ('Python', (3,12)<=python_version<(3,15),'需要Python 3.12–3.14'),
        ('Docker引擎',os_type=='linux','Docker Desktop需切换到Linux容器并启动引擎'),
        ('Docker内存',docker_memory>=2*GiB,'Docker可用总内存至少2GiB，建议4GiB以上'),
        ('本机可用内存',available_memory>=2*GiB,'本机可用内存至少2GiB，请关闭占用较高的程序'),
        ('项目磁盘空间',disk_free>=4*GiB,'项目所在磁盘至少保留4GiB空闲空间')]]

def memory_available():
    if os.name=='nt':
        import ctypes
        class Memory(ctypes.Structure):
            _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong)]+[(n,ctypes.c_ulonglong) for n in ('total','available','total_page','available_page','total_virtual','available_virtual','extended')]
        m=Memory();m.length=ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):raise RuntimeError('无法读取本机内存状态')
        return m.available
    return os.sysconf('SC_AVPHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')

def platform_owned():
    # Health alone is not identity. Validate the PID written by our launcher.
    try:
        pid=int((ROOT/'.runtime/platform.pid').read_text().strip())
        if pid<=0 or os.name!='nt':return False
        output=subprocess.run(['powershell','-NoProfile','-Command',f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress'],capture_output=True,text=True,timeout=8,check=True).stdout
        row=json.loads(output)
        if Path(row['ExecutablePath']).resolve()!= (ROOT/'.venv/Scripts/python.exe').resolve() or 'opslens.api:app' not in row['CommandLine']:return False
        import httpx
        h=httpx.get('http://127.0.0.1:18190/api/v1/health',trust_env=False,timeout=2).json()
        return h.get('status')=='ok' and h.get('delivery') in ('1.1','1.2','1.3','2.1','2.2','2.3','3.1','3.2','3.3')
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):return False
    except Exception:return False

def open_port(port):
    with socket.socket() as sock:sock.settimeout(.5);return sock.connect_ex(('127.0.0.1',port))==0

def check_ports(scope,owned,is_open=open_port,workbench_owned=False):
    if scope not in SCOPES:raise ValueError('未知启动范围')
    for port in SCOPES[scope]:
        if is_open(port) and port not in owned and not(port==18190 and workbench_owned):
            raise RuntimeError(f'端口{port}被其他服务占用；请先自行处理冲突，本脚本不会关闭它。')

def inspect_environment(scope='Platform'):
    if scope not in SCOPES:raise ValueError('未知启动范围')
    info=json.loads(docker(['info','--format','{{json .}}']))
    checks=resource_checks(info.get('OSType'),info.get('MemTotal',0),memory_available(),shutil.disk_usage(ROOT).free,sys.version_info[:2])
    owned=set()
    projects=['opslens-platform']+(['opslens-lab-fault'] if scope in ('Fault','All') else [])+(['opslens-lab-healthy'] if scope=='All' else [])
    for project in projects:
        for cid in docker(['ps','-q','--filter','label=com.docker.compose.project='+project]).split():
            row=json.loads(docker(['inspect',cid]))[0]
            labels=row['Config'].get('Labels') or {}
            expected='platform' if project=='opslens-platform' else 'lab'
            if labels.get('com.docker.compose.project')!=project or labels.get('io.opslens.scope')!=expected:continue
            for bindings in row['NetworkSettings'].get('Ports',{}).values():
                for b in bindings or []:
                    if b['HostIp']=='127.0.0.1':owned.add(int(b['HostPort']))
    check_ports(scope,owned,workbench_owned=platform_owned())
    checks.append({'name':'固定端口','ok':True,'message':'所选范围端口可用或属于本项目'})
    return {'scope':scope,'ok':all(c['ok'] for c in checks),'checks':checks}
