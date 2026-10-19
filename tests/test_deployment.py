import pytest
from opslens.deployment import resource_checks, check_ports

GiB=1024**3

def test_resources_require_linux_daemon_memory_and_disk():
    assert all(c['ok'] for c in resource_checks('linux',4*GiB,8*GiB,10*GiB,(3,14)))
    for args in [('windows',4*GiB,8*GiB,10*GiB,(3,14)),('linux',GiB,8*GiB,10*GiB,(3,14)),('linux',4*GiB,GiB,10*GiB,(3,14)),('linux',4*GiB,8*GiB,GiB,(3,14)),('linux',4*GiB,8*GiB,10*GiB,(3,11))]:
        assert any(not c['ok'] for c in resource_checks(*args))

def test_port_owner_cannot_be_faked_by_health_response():
    with pytest.raises(RuntimeError,match='18190'):
        check_ports('Platform',set(),lambda p:True,False)
    check_ports('Platform',{18193},lambda p:True,True)

def test_default_scope_does_not_inspect_or_change_healthy_ports():
    seen=[]
    check_ports('Fault',set(),lambda p:seen.append(p) or False,False)
    assert set(seen)=={18190,18182,18193}
    with pytest.raises(ValueError):check_ports('project-one',set(),lambda p:False,False)


def test_pid_of_foreign_python_is_not_owned_even_if_health_looks_valid(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from opslens import deployment
    (tmp_path/'.runtime').mkdir();(tmp_path/'.runtime/platform.pid').write_text('4321')
    monkeypatch.setattr(deployment,'ROOT',tmp_path)
    monkeypatch.setattr(deployment.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout=json.dumps({'ExecutablePath':str(tmp_path/'other/python.exe'),'CommandLine':'python -m uvicorn opslens.api:app'})))
    import httpx
    def forbidden(*a,**kw):raise AssertionError('foreign process must be rejected before trusting health')
    monkeypatch.setattr(httpx,'get',forbidden)
    assert deployment.platform_owned() is False
