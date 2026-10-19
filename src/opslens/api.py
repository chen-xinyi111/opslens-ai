import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import UUID, uuid4
import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from opslens import db
from opslens.config import LABS, ROOT
from opslens.contracts import Collection, CollectionInput, Evidence, FaultCase, Health, LabJob, LabName, LabStatus, SubmitJob
from opslens.queue import worker
from opslens.collector import lab_status
from opslens.investigation.api import router as investigation_router
from opslens.investigation.worker import worker as investigation_worker
from opslens.rag.api import router as knowledge_router
from opslens.rag.worker import worker as knowledge_worker
from opslens.recovery.api import router as recovery_router
from opslens.recovery.worker import worker as recovery_worker
from opslens.evaluation.api import router as evaluation_router
from opslens.demo import router as demo_router
from opslens.external import router as external_router, initialize as initialize_external
from opslens.diagnostic.api import router as diagnostic_router
from opslens.diagnostic.store import initialize as initialize_diagnostic
from opslens.diagnostic.worker import worker as diagnostic_worker


@asynccontextmanager
async def lifespan(app):
    db.initialize()
    initialize_external()
    initialize_diagnostic()
    diagnostic_task=asyncio.create_task(diagnostic_worker())
    worker_task = asyncio.create_task(worker())
    knowledge_task = asyncio.create_task(knowledge_worker())
    investigation_task = asyncio.create_task(investigation_worker())
    recovery_task=asyncio.create_task(recovery_worker())
    yield
    worker_task.cancel()
    knowledge_task.cancel()
    investigation_task.cancel()
    recovery_task.cancel()
    diagnostic_task.cancel()
    with suppress(asyncio.CancelledError):
        await worker_task
    with suppress(asyncio.CancelledError):
        await knowledge_task
    with suppress(asyncio.CancelledError):
        await investigation_task
    with suppress(asyncio.CancelledError):
        await recovery_task
    with suppress(asyncio.CancelledError):
        await diagnostic_task


app = FastAPI(title="OpsLens AI", version="0.5.0", lifespan=lifespan)
app.include_router(knowledge_router)
app.include_router(investigation_router)
app.include_router(recovery_router)
app.include_router(evaluation_router)
app.include_router(demo_router)
app.include_router(external_router)
app.include_router(diagnostic_router)


@app.middleware("http")
async def local_origin(request, call_next):
    host = request.headers.get("host", "").split(":")[0]
    origin = request.headers.get("origin")
    if host not in ("127.0.0.1", "localhost", "testserver"):
        return JSONResponse(status_code=403, content={"detail": "仅接受本机访问"})
    if origin and origin not in ("http://127.0.0.1:18190", "http://localhost:18190"):
        return JSONResponse(status_code=403, content={"detail": "仅接受本机工作台同源请求"})
    return await call_next(request)


@app.exception_handler(psycopg.Error)
async def storage_error(request, exc):
    return JSONResponse(status_code=503, content={"detail": "平台数据库暂时不可用，请稍后重试"})


@app.get("/api/v1/health", response_model=Health)
def health():
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "delivery": "3.3", "version":app.version}


@app.get("/api/v1/labs", response_model=list[LabStatus])
async def labs():
    return await asyncio.gather(*(asyncio.to_thread(lab_status, lab) for lab in LabName))


async def lab_request(lab, method, path, body=None):
    try:
        async with httpx.AsyncClient(timeout=7, trust_env=False) as client:
            response = await client.request(method, LABS[lab]["url"] + path, json=body)
        if response.status_code >= 400:
            raise HTTPException(response.status_code, response.json().get("detail", "实验服务请求失败"))
        return response.json()
    except httpx.HTTPError:
        raise HTTPException(503, "实验服务暂时不可达") from None


@app.post("/api/v1/lab-jobs", response_model=LabJob)
async def submit_job(data: SubmitJob):
    return await lab_request(data.lab, "POST", "/jobs", data.model_dump(mode="json", exclude={"lab"}))


@app.get("/api/v1/lab-jobs/{lab}/{job_id}", response_model=LabJob)
async def get_job(lab: LabName, job_id: UUID):
    return await lab_request(lab, "GET", "/jobs/" + str(job_id))


@app.get("/api/v1/fault-cases", response_model=list[FaultCase])
def fault_cases():
    with db.connect() as conn:
        return conn.execute("SELECT * FROM fault_cases ORDER BY id").fetchall()


@app.post("/api/v1/collections", response_model=Collection, status_code=202)
def create_collection(data: CollectionInput):
    with db.connect() as conn:
        if data.request_id:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,3102))", (str(data.request_id),))
            old=conn.execute("SELECT * FROM collections WHERE request_id=%s",(data.request_id,)).fetchone()
            if old:
                if old['lab']!=data.lab:raise HTTPException(409,'请求编号已用于不同采集')
                old['evidence']=conn.execute('SELECT * FROM evidence WHERE collection_id=%s ORDER BY collected_at',(old['id'],)).fetchall()
                return old
        row = conn.execute("INSERT INTO collections(id,lab,request_id) VALUES (%s,%s,%s) RETURNING *", (uuid4(), data.lab,data.request_id)).fetchone()
        db.append_event(conn, row['id'], "queued", {"message": "采集任务已排队"})
    return row | {"evidence": []}


def collection_row(conn, collection_id):
    row = conn.execute("SELECT * FROM collections WHERE id=%s", (collection_id,)).fetchone()
    if not row:
        raise HTTPException(404, "采集任务不存在")
    return row


@app.get("/api/v1/collections/{collection_id}", response_model=Collection)
def get_collection(collection_id: UUID):
    with db.connect() as conn:
        row = collection_row(conn, collection_id)
        evidence = conn.execute("SELECT * FROM evidence WHERE collection_id=%s ORDER BY collected_at", (collection_id,)).fetchall()
    with db.connect() as conn:
        from opslens.durability import describe
        recovery=describe(conn,'collection',collection_id)
    return row | {"evidence": evidence, 'recovery':recovery}


@app.get("/api/v1/evidence/{evidence_id}", response_model=Evidence)
def get_evidence(evidence_id: UUID):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM evidence WHERE id=%s", (evidence_id,)).fetchone()
    if not row:
        raise HTTPException(404, "证据不存在")
    return row


@app.get("/api/v1/collections/{collection_id}/events", response_class=StreamingResponse,
         responses={200: {"content": {"text/event-stream": {}}, "description": "Persisted progress; Last-Event-ID and after replay"}})
async def stream_events(collection_id: UUID, request: Request, after: int = 0):
    try:
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
        if after < 0 or int(request.headers.get("last-event-id", "0")) < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "事件游标必须为非负整数") from None
    with db.connect() as conn:
        collection_row(conn, collection_id)

    async def events():
        nonlocal cursor
        while not await request.is_disconnected():
            with db.connect() as conn:
                task = collection_row(conn, collection_id)
                rows = conn.execute("SELECT * FROM collection_events WHERE collection_id=%s AND id>%s ORDER BY id", (collection_id, cursor)).fetchall()
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\ndata: {json.dumps(row, ensure_ascii=False, default=str)}\n\n"
            if task["status"] in ("completed", "partial", "failed"):
                return
            yield ": heartbeat\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/", include_in_schema=False)
def home():
    entry = ROOT / "frontend/dist/index.html"
    if not entry.is_file():
        raise HTTPException(503, "工作台尚未构建，请运行 Start.ps1")
    return FileResponse(entry)


assets = ROOT / "frontend/dist/assets"
if assets.is_dir():
    app.mount("/assets", StaticFiles(directory=assets), name="assets")
