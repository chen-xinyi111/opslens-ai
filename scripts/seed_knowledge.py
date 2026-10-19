"""Builtin operational manuals derived from delivery-1.1 source, never from fault oracles."""
import json
import sys
import time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from opslens import db
from opslens.rag import store

def seed():
    manifest=json.loads((ROOT/"data/knowledge/manifest.json").read_text(encoding="utf-8"))
    ids=[]
    for row in manifest:
        raw=(ROOT/"data/knowledge"/(row["slug"]+".md")).read_bytes()
        doc=store.ingest(row["slug"]+".md",raw,row["title"],row["source"],"manual-1.1",["v1"])
        ids.append(str(doc["id"]))
    with db.connect() as conn:
        current=conn.execute("SELECT document_ids FROM kb_indexes WHERE mode='keyword' AND status='ready' AND active").fetchone()
    if not current or not set(ids)<=set(current["document_ids"]):
        with httpx.Client(timeout=15,trust_env=False) as client:
            response=client.post("http://127.0.0.1:18190/api/v1/kb/indexes",json={"mode":"keyword"})
            response.raise_for_status()
            task_id=response.json()["id"]
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                current=client.get("http://127.0.0.1:18190/api/v1/kb/tasks/"+task_id).json()
                if current["status"]=="completed":break
                if current["status"] in ("failed","blocked"):raise RuntimeError(current["error"])
                time.sleep(.25)
            else:raise RuntimeError("Keyword index build timed out")
    print(f"Seeded {len(ids)} source-grounded manuals; keyword index is ready.")
if __name__=="__main__":seed()
