"""Real single-agent development baseline; records are not frozen-set accuracy."""
import json,sys,time,hashlib
from pathlib import Path
from uuid import uuid4
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
OUTPUT=ROOT/".runtime/investigation-baseline-1.3.json"
EXPECTED={
"worker_stopped":["worker","停止"],"database_stopped":["数据库"],"redis_stopped":["redis"],
"wrong_address":["redis","地址"],"transaction_blocked":["锁"],"wrong_version":["版本"]}
def wait(client,i):
    end=time.monotonic()+270
    while time.monotonic()<end:
        r=client.get("/api/v1/investigations/"+i);r.raise_for_status();value=r.json()
        if value["status"] in ("completed","failed","blocked"):return value
        time.sleep(.6)
    raise RuntimeError("Persistent investigation did not reach terminal status: "+i)
def probe(client,case,job_id=None,target="fault"):
    r=client.post("/api/v1/investigations",json={"request_id":str(uuid4()),"target":target,"question":"该实验实例最近的业务任务出现异常，请依据当前运行状态、任务记录与手册调查原因；信息不足请明确列出补充项。","job_id":job_id})
    r.raise_for_status();value=wait(client,r.json()["id"])
    cited=0
    if value["status"]=="completed":
        for finding in value["report"]["findings"]:
            for quote in finding["references"]:
                ref=next(x for x in value["references"] if x["id"]==quote["id"])
                actual=client.get(ref["url"]);actual.raise_for_status()
                text=actual.json()["content"] if ref["kind"]=="runtime" else actual.json()["page_text"]
                assert quote["quote"] in text
                cited+=1
    report=value.get("report") or {}
    root=(report.get("root_cause") or "").lower()
    match=report.get("assessment")=="insufficient" and bool(report.get("missing_information")) if target=="unconnected" else report.get("assessment")=="supported" and all(word in root for word in EXPECTED[case])
    return {"case":case,"target":target,"task":value,"citation_checks":cited,"expected_match":match}
def run(mode):
    records=json.loads(OUTPUT.read_text(encoding="utf-8")) if OUTPUT.exists() else []
    with httpx.Client(base_url="http://127.0.0.1:18190",trust_env=False,timeout=25) as client:
        status=client.get("/api/v1/investigations/status").json()
        if not status["available"]:raise RuntimeError(status["reason"])
        # The baseline corpus must be the nine generated demo manuals only.
        docs=client.get("/api/v1/kb/documents").json()
        indexes=client.get("/api/v1/kb/indexes").json()
        current=next(x for x in indexes if x["active"] and x["mode"]=="semantic" and x["status"]=="ready")
        selected=[d for d in docs if d["id"] in current["document_ids"]]
        manifest=json.loads((ROOT/"data/knowledge/manifest.json").read_text(encoding="utf-8"))
        assert len(selected)==len(manifest)==9
        for d in selected:
            m=next(x for x in manifest if x["source"]==d["source"])
            assert d["sha256"]==hashlib.sha256((ROOT/"data/knowledge"/(m["slug"]+".md")).read_bytes()).hexdigest()
        def add(value):
            records.append(value);OUTPUT.write_text(json.dumps(records,ensure_ascii=False,indent=2,default=str),encoding="utf-8")
            print(value["case"]+": "+value["task"]["status"]+"; expected_match="+str(value["expected_match"])+"; citations="+str(value["citation_checks"]),flush=True)
            if value["task"]["status"]!="completed":raise RuntimeError(value["task"]["error"])
        if mode=="pilot":
            case=next(x for x in client.get("/api/v1/fault-cases").json() if x["id"]=="worker_stopped")
            job=client.get("/api/v1/lab-jobs/fault/"+case["sample_job_id"]).json()
            assert job["text"]=="OpsLens AI makes evidence clear" and job["status"]=="queued"
            add(probe(client,"worker_stopped",case["sample_job_id"]))
        elif mode=="remaining":
            import lab_control
            try:
                for case in list(EXPECTED)[1:]:
                    prepared=lab_control.prepare(case)
                    add(probe(client,case,prepared["sample_job_id"]))
            finally:
                prepared=lab_control.prepare("worker_stopped")
                print("Restored acceptance fixture: worker stopped",flush=True)
        elif mode=="insufficient":add(probe(client,"unconnected",target="unconnected"))
        else:raise ValueError("Use pilot, remaining, or insufficient")
if __name__=="__main__":run(sys.argv[1] if len(sys.argv)>1 else "pilot")
