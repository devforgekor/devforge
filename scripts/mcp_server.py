#!/usr/bin/env python3
# Status: experimental
# Path: none — manual start (planned: systemd:devforge-mcp.service)
"""MCP Server — SSE-based tool server (protocol 2024-11-05).

Tools:
  - fact_search: Semantic search on review_facts (pgvector 4096d)
  - mem_save: Store AI conversation memory (main app conversations/turns)
  - mem_search: Search stored memories by text (pg_trgm)

Usage:
  python3 scripts/mcp_server.py                    # default :8000
  python3 scripts/mcp_server.py --port 8001        # custom port
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from typing import Any, Dict, List, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mcp_server")

app = FastAPI(title="DevForge MCP", version="0.2.0")

sessions: Dict[str, asyncio.Queue] = {}

EMBEDDER_PORT = 8081
EMBED_URL = f"http://127.0.0.1:{EMBEDDER_PORT}/v1/embeddings"
EMBED_TIMEOUT = 15
_http = httpx.AsyncClient(timeout=EMBED_TIMEOUT)

TOOLS = [
    {
        "name": "fact_search",
        "description": "review_facts 테이블에서 의미 기반 검색. 사실(fact) 단위로 저장된 지식, 결정, 관찰 내용을 벡터 유사도로 검색합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색할 질문 또는 키워드 (자연어)",
                },
                "limit": {
                    "type": "integer",
                    "description": "반환할 결과 수 (기본 10, 최대 50)",
                    "default": 10,
                },
                "fact_type": {
                    "type": "string",
                    "description": "팩트 유형 필터 (선택: marker, text, entity_scan, enrich_meta, verify_result, observation 등)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "mem_save",
        "description": "AI 대화 기록을 저장합니다. tag=분류, summary=요약, detail=대화내용(JSON).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {"type": "string", "description": "분류 키워드"},
                "summary": {"type": "string", "description": "대화 요약"},
                "detail": {"type": "string", "description": "전체 대화 (JSON: user_query + assistant_answer)"},
                "model": {"type": "string", "description": "AI 모델명"},
            },
            "required": ["tag", "summary", "detail"],
        },
    },
    {
        "name": "mem_search",
        "description": "저장된 대화를 텍스트 검색 (pg_trgm 유사도).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색어",
                },
                "tag": {
                    "type": "string",
                    "description": "분류 필터 (선택)",
                },
            },
            "required": ["query"],
        },
    },
]


# ── Embedding helper (async, httpx) ───────────────────────────────


async def _get_query_vector(query: str) -> Optional[List[float]]:
    """Get embedding vector from embedder API. Returns None on failure."""
    try:
        resp = await _http.post(
            EMBED_URL,
            json={"input": query, "model": "default"},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as e:
        logger.warning("Embedder unavailable: %s", e)
        return None


# ── DB helpers (sync → async bridge) ────────────────────────────


async def _fetch_json(sql: str) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(psql_json, sql)


async def _execute(sql: str) -> bool:
    return await asyncio.to_thread(psql_ok, sql)


# ── Tool implementations ─────────────────────────────────────────


async def _tool_fact_search(query: str, limit: int = 10,
                            fact_type: Optional[str] = None) -> Dict[str, Any]:
    """Semantic search on review_facts via embedding similarity."""
    if limit > 50:
        limit = 50
    if limit < 1:
        limit = 1

    vec = await _get_query_vector(query)
    if vec is None:
        return {"error": "Embedder unavailable (try again when Pod B has embedder loaded)"}

    # Format as pgvector literal, esc_sql for safety
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
    type_filter = ""
    if fact_type:
        safe_type = fact_type.replace("'", "''")
        type_filter = f"AND rf.fact_type = '{safe_type}'"

    sql = f"""
        SELECT rf.id, rf.turn_id, rf.fact_type, rf.evidence,
               rf.embedding <=> '{esc_sql(vec_str)}'::vector AS distance,
               t.text_clean, t.created_at::text AS turn_created
        FROM review_facts rf
        LEFT JOIN turns t ON t.id = rf.turn_id
        WHERE rf.embedding IS NOT NULL
          AND rf.evidence IS NOT NULL
          {type_filter}
        ORDER BY rf.embedding <=> '{esc_sql(vec_str)}'::vector
        LIMIT {limit}
    """

    rows = await _fetch_json(sql)
    if not rows:
        return {"count": 0, "results": []}

    results = []
    for r in rows:
        evidence = r.get("evidence", "") or ""
        text_clean = r.get("text_clean") or ""
        results.append({
            "fact_id": str(r["id"]),
            "turn_id": str(r["turn_id"]),
            "fact_type": r.get("fact_type", ""),
            "evidence": evidence[:500],
            "distance": round(float(r["distance"]), 4),
            "context": text_clean[:300],
            "turn_created": r.get("turn_created", ""),
        })

    return {"count": len(results), "results": results}


async def _tool_mem_save(tag: str, summary: str, detail: str,
                         model: Optional[str] = None) -> Dict[str, Any]:
    """Store a memory in the main app's conversations/turns tables."""
    if not tag or not summary or not detail:
        return {"error": "tag, summary, detail are all required"}

    parsed = _parse_detail(detail)
    user_query = parsed.get("user_query", detail)[:2000]
    assistant_answer = parsed.get("assistant_answer", "")[:4000]
    reasoning = parsed.get("reasoning") or ""

    # Create conversation + turn in one go
    cid = await _fetch_json(
        f"INSERT INTO conversations (title, source, model) VALUES ("
        f"  '{esc_sql(summary[:200])}', '{esc_sql(tag)}', "
        f"  '{esc_sql(model or '')}'::text) RETURNING id"
    )
    if not cid:
        return {"error": "Failed to create conversation"}
    conversation_id = cid[0]["id"]

    # Next seq
    seq_row = await _fetch_json(
        f"SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
        f"FROM turns WHERE conversation_id = '{esc_sql(conversation_id)}'::uuid"
    )
    seq = seq_row[0]["next_seq"] if seq_row else 1

    turn_id = await _fetch_json(
        f"INSERT INTO turns (conversation_id, seq, user_turn, text, thinking, agent) "
        f"VALUES ("
        f"  '{esc_sql(conversation_id)}'::uuid, {seq}, "
        f"  '{esc_sql(user_query)}', '{esc_sql(assistant_answer)}', "
        f"  '{esc_sql(reasoning)}', '{esc_sql(tag)}'"
        f") RETURNING id"
    )
    tid = turn_id[0]["id"] if turn_id else None

    return {
        "status": "saved",
        "conversation_id": conversation_id,
        "turn_id": tid,
        "seq": seq,
    }


async def _tool_mem_search(query: str, tag: Optional[str] = None) -> Dict[str, Any]:
    """Search stored memories via pg_trgm similarity on turns.text."""
    tag_filter = ""
    if tag:
        safe_tag = tag.replace("'", "''")
        tag_filter = f"AND t.agent = '{safe_tag}'"

    sql = f"""
        SELECT t.id, t.conversation_id, t.seq, t.user_turn, t.text,
               c.title, c.source, c.model, c.created_at::text
        FROM turns t
        JOIN conversations c ON t.conversation_id = c.id
        WHERE t.text % '{esc_sql(query[:200])}'
          {tag_filter}
        ORDER BY similarity(t.text, '{esc_sql(query[:200])}') DESC
        LIMIT 20
    """
    rows = await _fetch_json(sql)
    if not rows:
        return {"count": 0, "results": []}

    results = []
    for r in rows:
        results.append({
            "turn_id": str(r["id"]),
            "conversation_id": str(r["conversation_id"]),
            "title": r.get("title") or "",
            "source": r.get("source") or "",
            "model": r.get("model") or "",
            "seq": r.get("seq"),
            "user_query": (r.get("user_turn") or "")[:300],
            "assistant_answer": (r.get("text") or "")[:500],
            "created_at": r.get("created_at", ""),
        })

    return {"count": len(results), "results": results}


def _parse_detail(detail: str) -> Dict[str, Any]:
    try:
        return json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return {"user_query": detail, "assistant_answer": "", "reasoning": ""}


# ── Helpers ──────────────────────────────────────────────────────


def _rpc_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


class _JSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, cls=_JSONEncoder)


# ── HTTP Endpoints ────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/mcp/sse")
async def mcp_sse():
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue

    async def event_stream():
        try:
            yield f"event: endpoint\ndata: /mcp/messages?session_id={session_id}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=300)
                    yield f"data: {_json_dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            sessions.pop(session_id, None)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/mcp/messages")
async def mcp_messages(session_id: str = Query(...), request: Request = None):
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError, TypeError):
        return JSONResponse(_rpc_error(None, -32700, "Parse error"))

    req_id = body.get("id")
    method = body.get("method", "")

    try:
        if method == "initialize":
            return JSONResponse(_rpc_result(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "devforge-mcp", "version": "0.2.0"},
            }))

        if method == "notifications/initialized":
            return JSONResponse({})

        if method == "tools/list":
            return JSONResponse(_rpc_result(req_id, {"tools": TOOLS}))

        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name == "fact_search":
                result = await _tool_fact_search(
                    query=arguments.get("query", ""),
                    limit=arguments.get("limit", 10),
                    fact_type=arguments.get("fact_type"),
                )
            elif tool_name == "mem_search":
                result = await _tool_mem_search(
                    query=arguments.get("query", ""),
                    tag=arguments.get("tag"),
                )
            elif tool_name == "mem_save":
                result = await _tool_mem_save(
                    tag=arguments.get("tag", ""),
                    summary=arguments.get("summary", ""),
                    detail=arguments.get("detail", ""),
                    model=arguments.get("model"),
                )
            else:
                result = {"error": f"Unknown tool: {tool_name}"}

            return JSONResponse(
                _rpc_result(req_id, {
                    "content": [{"type": "text", "text": _json_dumps(result)}]
                })
            )

        if method == "ping":
            return JSONResponse(_rpc_result(req_id, {}))

        logger.warning("Unknown MCP method: %s", method)
        return JSONResponse(_rpc_error(req_id, -32601, f"Method not found: {method}"))

    except Exception as e:
        logger.exception("MCP message error")
        return JSONResponse(_rpc_error(req_id, -32603, str(e)))


# ── Entry point ──────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(description="DevForge MCP Server")
    parser.add_argument("--port", "-p", type=int, default=8000,
                        help="Port to listen on (default: 8000)")
    args = parser.parse_args()
    logger.info("Starting MCP server on :%d", args.port)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
