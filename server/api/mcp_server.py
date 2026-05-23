import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, StreamingResponse

from .async_pg import init_db
from .ingest import router as ingest_router
from .slack_operator import router as slack_router
from .search import save_memory, search_memories
from .stats import get_stats, record_api_call

logger = logging.getLogger(__name__)

app = FastAPI(title="DevForge", version="0.1.0")
app.include_router(ingest_router)
app.include_router(slack_router)


@app.middleware("http")
async def _stats_middleware(request: Request, call_next):
    t0 = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - t0) * 1000
    record_api_call(elapsed_ms)
    return response

sessions: Dict[str, asyncio.Queue] = {}

TOOLS = [
    {
        "name": "mem_save",
        "description": "AI 대화를 저장합니다. tag는 분류 키워드, summary는 대화 요약, detail은 전체 대화 내용입니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "분류 키워드 (e.g., claude, copilot, gemini, python, devops)",
                },
                "summary": {
                    "type": "string",
                    "description": "대화 요약 (대화 제목으로 사용)",
                },
                "detail": {
                    "type": "string",
                    "description": "전체 대화 내용 (user/thinking/text JSON 구조)",
                },
                "model": {
                    "type": "string",
                    "description": "사용한 AI 모델명 (예: claude-sonnet-4-6)",
                },
                "type": {
                    "type": "string",
                    "description": "턴 유형: turn, decision, summary, error, code",
                },
                "tokens": {
                    "type": "integer",
                    "description": "총 토큰 사용량 (prompt + completion)",
                },
                "latency_ms": {
                    "type": "integer",
                    "description": "LLM 응답 시간 (밀리초)",
                },
                "tools_used": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "호출된 MCP 도구 목록",
                },
                "user_input_at": {
                    "type": "string",
                    "description": "사용자 입력 시각 (ISO 8601, 클라이언트 timestamp)",
                },
                "assistant_at": {
                    "type": "string",
                    "description": "AI 응답 완료 시각 (ISO 8601, 클라이언트 timestamp)",
                },
            },
            "required": ["tag", "summary", "detail"],
        },
    },
    {
        "name": "mem_search",
        "description": "저장된 AI 대화를 검색합니다. query는 검색어, tag는 선택적 필터입니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "검색어 (pg_trgm similarity 검색)",
                },
                "tag": {
                    "type": "string",
                    "description": "분류 키워드로 필터링 (선택사항)",
                },
            },
            "required": ["query"],
        },
    },
]


def _parse_detail(detail: str) -> Dict[str, Any]:
    try:
        return json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        return {"user_turn": detail, "text": ""}


@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("DevForge MCP server started")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/stats")
async def stats_endpoint(range: str = Query(default="7d")):
    if range not in ("1d", "7d", "30d", "all"):
        return JSONResponse({"error": "range must be 1d, 7d, 30d, or all"}, status_code=400)
    return await get_stats(range)


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
            return JSONResponse(
                _rpc_result(
                    req_id,
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "devforge", "version": "0.1.0"},
                    },
                )
            )

        if method == "notifications/initialized":
            return JSONResponse({})

        if method == "tools/list":
            return JSONResponse(_rpc_result(req_id, {"tools": TOOLS}))

        if method == "tools/call":
            params = body.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name == "mem_search":
                result = await _tool_mem_search(
                    arguments.get("query", ""), arguments.get("tag")
                )
            elif tool_name == "mem_save":
                result = await _tool_mem_save(
                    tag=arguments.get("tag", ""),
                    summary=arguments.get("summary", ""),
                    detail=arguments.get("detail", ""),
                    model=arguments.get("model"),
                    meta_type=arguments.get("type"),
                    tokens=arguments.get("tokens"),
                    latency_ms=arguments.get("latency_ms"),
                    tools_used=arguments.get("tools_used"),
                    user_input_at=arguments.get("user_input_at"),
                    assistant_at=arguments.get("assistant_at"),
                )
            else:
                result = {"error": f"Unknown tool: {tool_name}"}

            return JSONResponse(
                _rpc_result(req_id, {"content": [{"type": "text", "text": _json_dumps(result)}]})
            )

        if method == "ping":
            return JSONResponse(_rpc_result(req_id, {}))

        logger.warning("Unknown MCP method: %s", method)
        return JSONResponse(_rpc_error(req_id, -32601, f"Method not found: {method}"))

    except Exception as e:
        logger.exception("MCP message error")
        return JSONResponse(_rpc_error(req_id, -32603, str(e)))


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


async def _tool_mem_search(query: str, tag: Optional[str] = None) -> Dict[str, Any]:
    if not query:
        return {"error": "query is required"}
    rows = await search_memories(query=query, source=tag, limit=20)
    return {
        "count": len(rows),
        "results": [
            {
                "conversation_id": str(r["conversation_id"]),
                "title": r["title"],
                "source": r["source"],
                "model": r["model"],
                "seq": r["seq"],
                "user_turn": r["user_turn"],
                "text": r["text"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
            for r in rows
        ],
    }


async def _tool_mem_save(
    tag: str,
    summary: str,
    detail: str,
    model: Optional[str] = None,
    meta_type: Optional[str] = None,
    tokens: Optional[int] = None,
    latency_ms: Optional[int] = None,
    tools_used: Optional[List[str]] = None,
    user_input_at: Optional[str] = None,
    assistant_at: Optional[str] = None,
) -> Dict[str, Any]:
    if not tag or not summary or not detail:
        return {"error": "tag, summary, detail are all required"}

    parsed = _parse_detail(detail)

    user_turn = parsed.get("user_turn", detail)
    text = parsed.get("text", "")
    thinking = parsed.get("thinking")
    meta = parsed.get("meta", {})

    if meta_type:
        meta["type"] = meta_type
    if tokens is not None:
        meta["tokens"] = tokens
    if latency_ms is not None:
        meta["latency_ms"] = latency_ms
    if tools_used:
        meta["tools_used"] = tools_used
    if user_input_at:
        meta["user_input_at"] = user_input_at
    if assistant_at:
        meta["assistant_at"] = assistant_at

    result = await save_memory(
        source=tag,
        user_turn=user_turn,
        text=text or summary,
        title=summary,
        model=model,
        thinking=thinking,
        meta=meta,
        wing=parsed.get("wing"),
        room=parsed.get("room"),
    )
    result["status"] = "saved"
    return result
