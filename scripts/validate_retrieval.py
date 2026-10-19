"""Six development retrieval probes; never claim model quality from fixture tests."""
import json
import hashlib
import sys
import time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from opslens import db

def wait(client,task_id,timeout=120):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        response=client.get("/api/v1/kb/tasks/"+task_id);response.raise_for_status()
        value=response.json()
        if value["status"] in ("completed","failed","blocked"):return value
        time.sleep(.3)
    raise RuntimeError("Retrieval task timeout; inspect persistent task by ID")

def evaluate(modes):
    cases=json.loads((ROOT/"data/knowledge/development-queries.json").read_text(encoding="utf-8"))
    records=[]
    with httpx.Client(base_url="http://127.0.0.1:18190",timeout=15,trust_env=False) as client:
        status=client.get("/api/v1/kb/status").json()
        semantic_id=None
        if any(mode!="keyword" for mode in modes):
            if not status["models"]["text-embedding-v4"]["available"]:
                raise RuntimeError("Embedding free quota has not been verified; no model calls were made.")
            if "rerank" in modes and not status["models"]["gte-rerank-v2"]["available"]:
                raise RuntimeError("Rerank free quota has not been verified; no model calls were made.")
            # Only these reviewed, generated lab manuals may be sent by the pilot.
            # User uploads and runtime logs are excluded; fail on any content drift.
            manifest=json.loads((ROOT/"data/knowledge/manifest.json").read_text(encoding="utf-8"))
            documents=client.get("/api/v1/kb/documents");documents.raise_for_status()
            selected=[]
            for entry in manifest:
                expected_hash=hashlib.sha256((ROOT/"data/knowledge"/(entry["slug"]+".md")).read_bytes()).hexdigest()
                matching=[d for d in documents.json() if d["source"]==entry["source"] and d["doc_version"]=="manual-1.1" and d["sha256"]==expected_hash]
                if len(matching)!=1:raise RuntimeError("Builtin manual content differs from reviewed source; stop model pilot.")
                selected.append(matching[0]["id"])
            response=client.post("/api/v1/kb/indexes",json={"mode":"semantic","document_ids":selected});response.raise_for_status()
            index=wait(client,response.json()["id"])
            if index["status"]!="completed":raise RuntimeError(index["error"])
            semantic_id=index["result"]["index_id"]
        for mode in modes:
            for case in cases:
                response=client.post("/api/v1/kb/searches",json={"query":case["query"],"mode":mode,"app_version":"v1","index_id":semantic_id if mode!="keyword" else None});response.raise_for_status()
                task=wait(client,response.json()["id"])
                if task["status"]!="completed":raise RuntimeError(task["error"])
                result=task["result"]
                expected="opslens-ai/data/knowledge/"+case["source"]+".md"
                rank=next((i+1 for i,hit in enumerate(result["hits"]) if hit["document"]["source"]==expected),None)
                checked=[]
                for hit in result["hits"]:
                    cite=client.get(hit["citation_url"]);cite.raise_for_status()
                    actual=cite.json()
                    assert actual["chunk"]["content"]==hit["chunk"]["content"]
                    assert actual["document"]["doc_version"]==hit["document"]["doc_version"]
                    checked.append(str(hit["chunk"]["id"]))
                records.append({"mode":mode,"query":case["query"],"expected_source":expected,"expected_rank":rank,"citation_checks":len(checked),"task_id":task["id"],"result":result})
                print(f"{mode}: expected rank={rank}; citations={len(checked)}",flush=True)
    with db.connect() as conn:
        model_calls=conn.execute("SELECT model,status,actual_tokens,error_code,created_at FROM kb_model_calls ORDER BY created_at").fetchall()
        embeddings=conn.execute("SELECT count(*) AS n FROM kb_embeddings").fetchone()["n"]
    summary={"dataset":"six development questions, not frozen test-set quality","records":records,"model_calls":model_calls,"stored_vectors":embeddings,"verified_modes":modes,"all_citations_checked":True}
    path=ROOT/".runtime/retrieval-validation-1.2.json"
    path.write_text(json.dumps(summary,ensure_ascii=False,default=str,indent=2),encoding="utf-8")
    print(f"Saved {len(records)} real retrieval records; production model calls: {len(model_calls)}")
if __name__=="__main__":
    modes=sys.argv[1:] or ["keyword"]
    if not all(mode in ("keyword","vector","hybrid","rerank") for mode in modes):raise SystemExit("Unknown retrieval mode")
    evaluate(modes)
