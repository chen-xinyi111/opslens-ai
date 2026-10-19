"""Local CLI only. No fault mutation endpoint is exposed by the platform."""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from uuid import uuid4
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opslens.config import LABS, secrets
from opslens.collector import docker, containers, diagnostics
from opslens.security import redact

TEXT = "OpsLens AI makes evidence clear"
CASES = ("database_stopped","redis_stopped","worker_stopped","wrong_address","transaction_blocked","wrong_version")
client = httpx.Client(timeout=8, trust_env=False)


def compose(args, override=None, timeout=90):
    # Only the fixed fault project can be mutated by this controller.
    base = ["compose","-p","opslens-lab-fault","--env-file",str(ROOT/".runtime/fault.env"),"-f",str(ROOT/"compose/lab.yaml")]
    if override:
        base += ["-f",str(override)]
    return docker(base + args, timeout=timeout)


def wait_until(check, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(0.25)
    raise RuntimeError("Timed out waiting for an actual experiment state")


def submit(lab="fault", request_id=None):
    response = client.post(LABS[lab]["url"]+"/jobs",json={"text":TEXT,"request_id":str(request_id or uuid4())})
    return response


def completed(job_id, lab="fault"):
    def check():
        response = client.get(LABS[lab]["url"]+"/jobs/"+job_id)
        response.raise_for_status()
        row = response.json()
        return row if row["status"] == "completed" else None
    return wait_until(check, 25)


def probe(lab="fault"):
    response = submit(lab)
    response.raise_for_status()
    row = completed(response.json()["id"],lab)
    assert row["result"] == {"char_count":31,"word_count":5}, row
    assert row["worker_version"] == "v1", row
    return row


def restore():
    containers("fault")  # Identity validation before any mutations.
    compose(["--profile","fault-tools","stop","blocker"])
    compose(["up","-d","--wait","--wait-timeout","60"])
    compose(["up","-d","--no-deps","--force-recreate","worker"])
    return probe()


def override_worker(environment):
    target = ROOT/".runtime/fault.override.json"
    target.write_text(json.dumps({"services":{"worker":{"environment":environment}}}),encoding="utf-8")
    compose(["up","-d","--no-deps","--force-recreate","worker"],override=target)


def evidence_case(case_id, job_id):
    response = client.post("http://127.0.0.1:18190/api/v1/collections",json={"lab":"fault"})
    response.raise_for_status()
    collection_id = response.json()["id"]
    def check():
        result = client.get("http://127.0.0.1:18190/api/v1/collections/"+collection_id)
        result.raise_for_status()
        value = result.json()
        return value if value["status"] in ("completed","partial","failed") else None
    record = wait_until(check,60)
    if record["status"] == "failed":
        raise RuntimeError("Evidence collection failed")
    # Case metadata/oracle is stored separately from the evidence stream.
    from opslens import db
    with db.connect() as conn:
        conn.execute("UPDATE fault_cases SET collection_id=%s,sample_job_id=%s,observed_at=now() WHERE id=%s",(collection_id,job_id,case_id))
    return record


def prepare(case_id):
    if case_id not in CASES:
        raise ValueError("Unknown fixed fault case")
    restore()
    sample_id = None
    if case_id == "database_stopped":
        compose(["stop","postgres"])
        response = submit()
        assert response.status_code == 503, response.text
        observed = {"http_status":response.status_code}
    elif case_id == "redis_stopped":
        compose(["stop","redis"])
        response = submit()
        response.raise_for_status()
        row = response.json()
        sample_id = row["id"]
        assert row["status"] == "queued" and row["queue_error"] == "redis_unavailable", row
        observed = {"status":row["status"],"queue_error":row["queue_error"]}
    elif case_id in ("worker_stopped","wrong_address"):
        if case_id == "worker_stopped":
            compose(["stop","worker"])
        else:
            override_worker({"LAB_REDIS_URL":"redis://missing-opslens-redis.invalid:6379/0"})
        response = submit()
        response.raise_for_status()
        sample_id = response.json()["id"]
        if case_id == "wrong_address":
            def actual_failure():
                cid = next(cid for cid, service, _ in containers("fault") if service == "worker")
                logs = docker(["logs", cid])
                return "missing-opslens-redis.invalid" in logs and "failure" in logs
            wait_until(actual_failure, 45)
        else:
            time.sleep(2)
        row = client.get(LABS["fault"]["url"]+"/jobs/"+sample_id).json()
        assert row["status"] == "queued", row
        observed = {"status":row["status"]}
    elif case_id == "transaction_blocked":
        compose(["stop","worker"])
        response = submit()
        response.raise_for_status()
        sample_id = response.json()["id"]
        os.environ["BLOCK_JOB_ID"] = sample_id
        compose(["--profile","fault-tools","up","-d","--no-deps","--force-recreate","blocker"])
        def lock_ready():
            rows = containers("fault")
            cid = next(cid for cid,service,_ in rows if service == "blocker")
            return "LOCK_READY" in docker(["logs",cid])
        wait_until(lock_ready)
        compose(["start","worker"])
        def blocking():
            activity = diagnostics("fault")["database"]["activity"]
            waiting = [r for r in activity if r["application_name"] == "opslens-worker" and r["blocking_pids"]]
            return waiting or None
        observed = {"waiting_activity":wait_until(blocking)}
        row = client.get(LABS["fault"]["url"]+"/jobs/"+sample_id).json()
        assert row["status"] != "completed"
    else:
        override_worker({"LAB_WORKER_VERSION":"v0-bug"})
        response = submit()
        response.raise_for_status()
        sample_id = response.json()["id"]
        row = completed(sample_id)
        assert row["worker_version"] == "v0-bug" and row["result"]["word_count"] == 1, row
        observed = {"result":row["result"],"worker_version":row["worker_version"],"expected_word_count":5}
    collection = evidence_case(case_id, sample_id)
    if case_id == "wrong_address":
        worker_logs = "\n".join(e["content"] for e in collection["evidence"] if e["source"] == "docker.logs.worker")
        assert "missing-opslens-redis.invalid" in worker_logs and "failure" in worker_logs, worker_logs
    return {"case_id":case_id,"sample_job_id":sample_id,"observed":observed,"collection_id":collection["id"],"evidence_count":len(collection["evidence"]),"collection_status":collection["status"]}


def validate_all():
    other_ids = docker(["ps","-aq","--filter","label=com.docker.compose.project=agent-commerce-stage3"]).split()
    baseline = {cid: json.loads(docker(["inspect",cid]))[0]["State"]["StartedAt"] for cid in other_ids}
    records = []
    for case_id in CASES:
        print("Validating " + case_id,flush=True)
        try:
            record = prepare(case_id)
            recovered = restore()
            record["recovery_probe"] = {"status":recovered["status"],"result":recovered["result"],"worker_version":recovered["worker_version"]}
            record["healthy_probe"] = probe("healthy")["result"]
            record["passed"] = True
            records.append(record)
        except Exception:
            restore()
            raise
    prepare("worker_stopped")
    after = {cid: json.loads(docker(["inspect",cid]))[0]["State"]["StartedAt"] for cid in other_ids}
    assert baseline == after, "Project one was restarted"
    report = {"cases":records,"project_one_unchanged":baseline == after,"model_calls":0,"finished_at":time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    target = ROOT/".runtime/fault-validation.json"
    target.write_text(redact(json.dumps(report,ensure_ascii=False,indent=2,default=str),secrets()),encoding="utf-8")
    print("Six real faults and recovery probes passed. Worker-stopped acceptance case is ready.",flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action",choices=["prepare","restore","validate"])
    parser.add_argument("--case",choices=CASES,default="worker_stopped")
    args = parser.parse_args()
    try:
        if args.action == "validate": validate_all()
        elif args.action == "restore": print("Restored:",restore()["result"])
        else: print(json.dumps(prepare(args.case),ensure_ascii=False,default=str))
    finally:
        client.close()
