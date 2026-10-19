import json
import time
from uuid import UUID
from opslens import db
from opslens.durability import saved_provider, write
from opslens.rag import store
from opslens.rag.contracts import SearchResult, SearchHit, Document, Chunk
from opslens.rag.provider import DashScopeProvider, EMBED_MODEL, RERANK_MODEL, DIMENSIONS, ModelFailure
from opslens.rag.ranking import bm25, rrf


def vector_literal(values):
    return "["+",".join(format(float(value),".17g") for value in values)+"]"


def vectors(texts, kind="document", provider=None):
    provider=saved_provider(provider or DashScopeProvider())
    keys=[store.embedding_key(text,kind) for text in texts]
    output={}
    with db.connect() as conn:
        for key in set(keys):
            row=conn.execute("SELECT embedding::text AS value FROM kb_embeddings WHERE content_sha256=%s AND model=%s AND dimension=%s",(key,EMBED_MODEL,DIMENSIONS)).fetchone()
            if row: output[key]=json.loads(row["value"])
    missing=sorted(set(key for key in keys if key not in output))
    for start in range(0,len(missing),10):
        batch=missing[start:start+10]
        batch_text=[texts[keys.index(key)] for key in batch]
        response=provider.embed(batch_text,text_type=kind)
        with write() as conn:
            for key,value in zip(batch,response,strict=True):
                output[key]=value
                conn.execute("INSERT INTO kb_embeddings(content_sha256,model,dimension,embedding) VALUES (%s,%s,%s,%s::vector) ON CONFLICT DO NOTHING",(key,EMBED_MODEL,DIMENSIONS,vector_literal(value)))
    return [output[key] for key in keys]


def build_index(index_id, provider=None):
    with db.connect() as conn:
        index=conn.execute("SELECT * FROM kb_indexes WHERE id=%s",(index_id,)).fetchone()
        if not index or index["status"] not in ("building","ready"):
            raise ValueError("索引不处于可构建状态")
        rows=conn.execute("SELECT c.* FROM kb_chunks c JOIN kb_index_chunks i ON i.chunk_id=c.id WHERE i.index_id=%s ORDER BY c.document_id,c.ordinal",(index_id,)).fetchall()
    if index["mode"]=="semantic" and index["status"]=="building":
        vectors([row["retrieval_text"] for row in rows],provider=provider)
    with write() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,1203))",(index["mode"],))
        # An earlier, slower build cannot displace a newer completed index.
        newer=conn.execute("SELECT 1 FROM kb_indexes WHERE mode=%s AND status='ready' AND created_at>%s LIMIT 1",(index["mode"],index["created_at"])).fetchone()
        if not newer:
            conn.execute("UPDATE kb_indexes SET active=false WHERE mode=%s",(index["mode"],))
        conn.execute("UPDATE kb_indexes SET status='ready',active=%s,finished_at=now() WHERE id=%s",(not bool(newer),index_id))
    return {"index_id":str(index_id),"mode":index["mode"],"chunks":len(rows),"embedding_model":index["embedding_model"],"dimensions":index["dimensions"]}


def search(payload, provider=None):
    started=time.perf_counter()
    query,mode,index_id,app_version=(payload[key] for key in ("query","mode","index_id","app_version"))
    provider=saved_provider(provider or DashScopeProvider())
    with db.connect() as conn:
        index=conn.execute("SELECT * FROM kb_indexes WHERE id=%s AND status='ready'",(index_id,)).fetchone()
        if not index: raise LookupError("索引不可用")
        rows=conn.execute("""
            SELECT c.*,d.title,d.source,d.doc_version,d.applicable_versions,d.filename,d.format,
                   d.sha256 AS document_sha256,d.created_at AS document_created_at
            FROM kb_chunks c JOIN kb_documents d ON d.id=c.document_id
            JOIN kb_index_chunks i ON i.chunk_id=c.id
            WHERE i.index_id=%s AND (d.applicable_versions ? %s OR d.applicable_versions ? '*')
            ORDER BY c.document_id,c.ordinal
        """,(index_id,app_version)).fetchall()
    keyword=bm25(query,[{"id":row["id"],"text":row["retrieval_text"]} for row in rows]) if mode!="vector" else []
    vector=[]
    if mode!="keyword" and rows:
        if index["mode"]!="semantic":
            raise ValueError("索引没有真实向量")
        query_vector=vectors([query],"query",provider)[0]
        with db.connect() as conn:
            vector=conn.execute("""
                SELECT c.id,1-(e.embedding <=> %s::vector) AS score FROM kb_chunks c
                JOIN kb_index_chunks i ON i.chunk_id=c.id JOIN kb_documents d ON d.id=c.document_id
                JOIN kb_embeddings e ON e.content_sha256=c.embedding_key AND e.model=%s AND e.dimension=%s
                WHERE i.index_id=%s AND (d.applicable_versions ? %s OR d.applicable_versions ? '*')
                ORDER BY e.embedding <=> %s::vector,c.id LIMIT 20
            """,(vector_literal(query_vector),EMBED_MODEL,DIMENSIONS,index_id,app_version,vector_literal(query_vector))).fetchall()
    candidates=rrf(keyword,vector)
    by_id={str(row["id"]):row for row in rows}
    reranked=0
    if mode=="rerank" and candidates:
        while candidates and len(query.encode())*len(candidates)+sum(len(by_id[item["id"]]["retrieval_text"].encode()) for item in candidates)>29000:
            candidates.pop()
        if not candidates:
            raise ModelFailure("问题与文档长度超出重排预算")
        ranks=provider.rerank(query,[by_id[item["id"]]["retrieval_text"] for item in candidates])
        reranked=len(ranks)
        for row in ranks:
            candidates[row["index"]]["rerank_score"]=row["relevance_score"]
        candidates.sort(key=lambda item:(-item["rerank_score"],item["id"]))
    hits=[]
    for candidate in candidates[:6]:
        row=by_id[candidate["id"]]
        doc={"id":row["document_id"],"title":row["title"],"source":row["source"],"doc_version":row["doc_version"],"applicable_versions":row["applicable_versions"],"filename":row["filename"],"format":row["format"],"sha256":row["document_sha256"],"created_at":row["document_created_at"]}
        hits.append(SearchHit(chunk=Chunk.model_validate(row),document=Document.model_validate(doc),index_id=index_id,citation_url="/api/v1/kb/citations/"+candidate["id"],**{key:value for key,value in candidate.items() if key!="id"}))
    return SearchResult(query=query,mode=mode,app_version=app_version,index_id=index_id,hits=hits,
        counts={"bm25":len(keyword),"vector":len(vector),"fused":len(candidates),"reranked":reranked},
        elapsed_ms=round((time.perf_counter()-started)*1000,2),
        embedding_model=EMBED_MODEL if mode!="keyword" else None,rerank_model=RERANK_MODEL if reranked else None,
        note="候选资料用于核对原文，不代表已经确认故障原因。").model_dump(mode="json")
