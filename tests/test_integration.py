"""Integration checks use only OpsLens fixtures; no project-one writes."""
import concurrent.futures
import json
import time
from uuid import uuid4
import httpx
import pytest
from opslens.config import secrets
from opslens import db

pytestmark = pytest.mark.integration
BASE = "http://127.0.0.1:18190"


@pytest.fixture(scope="module")
def client():
    with httpx.Client(base_url=BASE,timeout=30,trust_env=False) as c:
        yield c


def wait_job(client, job_id):
    for _ in range(60):
        row = client.get(f"/api/v1/lab-jobs/healthy/{job_id}").json()
        if row["status"] == "completed":
            return row
        time.sleep(.2)
    pytest.fail("Healthy worker did not process the real task")


def test_real_job_repeated_request_and_instance_isolation(client):
    request_id = str(uuid4())
    body = {"lab":"healthy","request_id":request_id,"text":"OpsLens AI makes evidence clear"}
    response = client.post("/api/v1/lab-jobs",json=body)
    assert response.status_code == 200
    job_id = response.json()["id"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post("/api/v1/lab-jobs",json=body),range(4)))
    assert all(r.status_code == 200 and r.json()["id"] == job_id for r in responses)
    assert wait_job(client,job_id)["result"] == {"char_count":31,"word_count":5}
    assert client.post("/api/v1/lab-jobs",json=body|{"text":"different"}).status_code == 409
    assert client.get(f"/api/v1/lab-jobs/fault/{job_id}").status_code == 404
    assert client.get(f"/api/v1/lab-jobs/healthy/{uuid4()}").status_code == 404


def test_persistent_collection_sse_replay_and_secret_redaction(client):
    response = client.post("/api/v1/collections",json={"lab":"healthy"})
    assert response.status_code == 202
    cid = response.json()["id"]
    with client.stream("GET",f"/api/v1/collections/{cid}/events") as streamed:
        assert streamed.status_code == 200
        assert streamed.headers["content-type"].startswith("text/event-stream")
        lines = list(streamed.iter_lines())
    events = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    ids = [event["id"] for event in events]
    assert ids == sorted(set(ids)) and events[0]["kind"] == "queued" and events[-1]["kind"] == "finished"
    replay = client.get(f"/api/v1/collections/{cid}/events",headers={"Last-Event-ID":str(ids[0])})
    replay_events = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
    assert [event["id"] for event in replay_events] == ids[1:]
    row = client.get(f"/api/v1/collections/{cid}").json()
    assert row["status"] == "completed"
    assert len(row["evidence"]) >= 6
    with db.connect() as conn:
        stored_count = conn.execute("SELECT count(*) AS n FROM evidence WHERE collection_id=%s",(cid,)).fetchone()["n"]
    assert stored_count == len(row["evidence"])
    joined = json.dumps(row,ensure_ascii=False)
    for secret in secrets():
        if secret: assert secret not in joined
    for item in row["evidence"]:
        actual = client.get("/api/v1/evidence/"+item["id"])
        assert actual.status_code == 200 and actual.json()["sha256"] == item["sha256"]
        assert actual.json()["collected_at"] == item["collected_at"]
    assert client.get(f"/api/v1/evidence/{uuid4()}").status_code == 404
    assert client.get(f"/api/v1/collections/{uuid4()}").status_code == 404


def test_diagnostics_auth_and_platform_origin(client):
    assert httpx.get("http://127.0.0.1:18181/diagnostics",trust_env=False).status_code == 403
    assert client.post("/api/v1/collections",json={"lab":"healthy"},headers={"Origin":"https://evil.example"}).status_code == 403
    assert client.post("/api/v1/collections",json={"lab":"outside"}).status_code == 422
    assert client.get("/api/v1/health").json()["delivery"] == "3.3"

def test_network_volume_and_port_isolation():
    from opslens.collector import containers, docker
    groups = {lab: containers(lab) for lab in ("healthy","fault")}
    networks = {}
    volumes = {}
    for lab, rows in groups.items():
        networks[lab] = {name for _, _, obj in rows for name in obj["NetworkSettings"]["Networks"]}
        volumes[lab] = {mount["Name"] for _, _, obj in rows for mount in obj.get("Mounts",[]) if mount["Type"] == "volume"}
        for _, service, obj in rows:
            for bindings in obj["HostConfig"].get("PortBindings",{}).values():
                for binding in bindings or []:
                    assert binding["HostIp"] == "127.0.0.1"
            if service in ("postgres","redis"):
                assert not obj["HostConfig"].get("PortBindings")
    assert networks["healthy"].isdisjoint(networks["fault"])
    assert volumes["healthy"].isdisjoint(volumes["fault"])
    platform_ids = docker(["ps","-q","--filter","label=com.docker.compose.project=opslens-platform"]).split()
    assert len(platform_ids) == 1
    platform = json.loads(docker(["inspect",platform_ids[0]]))[0]
    assert set(platform["NetworkSettings"]["Networks"]).isdisjoint(networks["healthy"] | networks["fault"])
    assert {mount["Name"] for mount in platform["Mounts"] if mount["Type"] == "volume"}.isdisjoint(volumes["healthy"] | volumes["fault"])
