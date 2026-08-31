#!/usr/bin/env python3.11
# Status: experimental
# Path: systemd:devforge-mcp.service
"""DevForge MCP Server — Streamable HTTP (MCP spec 2025-03-26).

Tools:
  - fact_search: Semantic search on review_facts (pgvector 4096d)
  - mem_save: Store AI conversation memory
  - mem_search: Search stored memories by text (pg_trgm)
  - search_similarity: Hybrid BM25+Dense semantic search on turns (RRF fusion)
  - search_conversations: List/search conversations
  - get_conversation: Get full conversation thread
  - search_turns: Search turns with filters
  - get_turn_facts: Get all facts for a turn
  - ingest: Batch store conversation + turns
  - review_sequential: Sequential code review for Aider task prep

Usage:
  python3.11 mcp_server.py                    # default :8000
  python3.11 mcp_server.py --port 8001        # custom port
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import httpx
from fastmcp import FastMCP
from lib.db import esc_sql, psql_json, psql_ok

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("mcp_server")

mcp = FastMCP(name="devforge-mcp")

EMBEDDER_PORT = 8081
EMBED_URL = f"http://127.0.0.1:{EMBEDDER_PORT}/v1/embeddings"
_http = httpx.AsyncClient(timeout=15)


# ── Embedding helper ───────────────────────────────────────────


async def _get_query_vector(query: str) -> Optional[list[float]]:
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


async def _fetch_json(sql: str) -> list[dict]:
    """Bridge sync psql_json to async via thread pool."""
    return await asyncio.to_thread(psql_json, sql)


async def _execute(sql: str) -> bool:
    return await asyncio.to_thread(psql_ok, sql)


# ── Tools ──────────────────────────────────────────────────────


@mcp.tool(name="fact_search")
async def fact_search(query: str, limit: int = 10, fact_type: Optional[str] = None) -> str:
    """review_facts 테이블에서 의미 기반 검색. 벡터 유사도로 관련 fact를 찾습니다.

    Args:
        query: 검색할 질문 또는 키워드 (자연어)
        limit: 반환할 결과 수 (기본 10, 최대 50)
        fact_type: 팩트 유형 필터 (marker, text, entity_scan, enrich_meta, verify_result, observation 등)
    """
    limit = max(1, min(limit, 50))

    vec = await _get_query_vector(query)
    if vec is None:
        return json.dumps(
            {"error": "Embedder unavailable (try again when inference has embedder loaded)"},
            ensure_ascii=False,
        )

    vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"
    type_filter = ""
    if fact_type:
        type_filter = f"AND rf.fact_type = '{esc_sql(fact_type)}'"

    rows = await _fetch_json(f"""
        SELECT rf.id, rf.turn_id, rf.fact_type, rf.evidence,
               e.embedding <=> '{esc_sql(vec_str)}'::vector AS distance,
               t.text_clean, t.created_at::text AS turn_created
        FROM review_facts rf
        JOIN embeddings e ON e.source_type = 'review_fact' AND e.source_id = rf.id
          AND e.model_name = 'qwen3-embedding-8b-v1'
        LEFT JOIN turns t ON t.id = rf.turn_id
        WHERE e.embedding IS NOT NULL
          AND rf.evidence IS NOT NULL
          {type_filter}
        ORDER BY e.embedding <=> '{esc_sql(vec_str)}'::vector
        LIMIT {limit}
    """)
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)

    results = []
    for r in rows:
        results.append(
            {
                "fact_id": str(r["id"]),
                "turn_id": str(r["turn_id"]),
                "fact_type": r.get("fact_type", ""),
                "evidence": (r.get("evidence") or "")[:500],
                "distance": round(float(r["distance"]), 4),
                "context": (r.get("text_clean") or "")[:300],
                "turn_created": r.get("turn_created", ""),
            }
        )
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


@mcp.tool(name="mem_save")
async def mem_save(tag: str, summary: str, detail: str, model: Optional[str] = None) -> str:
    """AI 대화 기록을 저장합니다. tag=분류, summary=요약, detail=대화내용.

    Args:
        tag: 분류 키워드 (예: claude, debug, design, review)
        summary: 대화 요약 (200자 이내)
        detail: 전체 대화 내용. JSON 형태 권장: {"user_query": "...", "assistant_answer": "...", "reasoning": "..."}
        model: AI 모델명 (선택)
    """
    if not tag or not summary or not detail:
        return json.dumps({"error": "tag, summary, detail are all required"}, ensure_ascii=False)

    try:
        parsed = json.loads(detail)
    except (json.JSONDecodeError, TypeError):
        parsed = {"user_query": detail, "assistant_answer": "", "reasoning": ""}

    user_query = (parsed.get("user_query") or detail)[:2000]
    assistant_answer = (parsed.get("assistant_answer") or "")[:4000]
    reasoning = parsed.get("reasoning") or ""

    cid = await _fetch_json(
        f"INSERT INTO conversations (title, source, model) VALUES ("
        f"'{esc_sql(summary[:200])}', '{esc_sql(tag)}', "
        f"'{esc_sql(model or '')}'::text) RETURNING id"
    )
    if not cid:
        return json.dumps({"error": "Failed to create conversation"}, ensure_ascii=False)
    conversation_id = cid[0]["id"]

    seq_row = await _fetch_json(
        f"SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
        f"FROM turns WHERE conversation_id = '{esc_sql(conversation_id)}'::uuid"
    )
    seq = seq_row[0]["next_seq"] if seq_row else 1

    turn_id = await _fetch_json(
        f"INSERT INTO turns (conversation_id, seq, user_turn, text, thinking, agent) "
        f"VALUES ('{esc_sql(conversation_id)}'::uuid, {seq}, "
        f"'{esc_sql(user_query)}', '{esc_sql(assistant_answer)}', "
        f"'{esc_sql(reasoning)}', '{esc_sql(tag)}') RETURNING id"
    )
    tid = str(turn_id[0]["id"]) if turn_id else None

    return json.dumps(
        {
            "status": "saved",
            "conversation_id": str(conversation_id),
            "turn_id": tid,
            "seq": seq,
        },
        ensure_ascii=False,
    )


@mcp.tool(name="mem_search")
async def mem_search(query: str, tag: Optional[str] = None) -> str:
    """저장된 대화를 텍스트 검색 (pg_trgm 유사도).

    Args:
        query: 검색어
        tag: 분류 필터 (agent 이름, 선택)
    """
    tag_filter = ""
    if tag:
        tag_filter = f"AND t.agent = '{esc_sql(tag)}'"

    rows = await _fetch_json(f"""
        SELECT t.id, t.conversation_id, t.seq, t.user_turn, t.text,
               c.title, c.source, c.model, c.created_at::text
        FROM turns t
        JOIN conversations c ON t.conversation_id = c.id
        WHERE t.text % '{esc_sql(query[:200])}'
          {tag_filter}
        ORDER BY similarity(t.text, '{esc_sql(query[:200])}') DESC
        LIMIT 20
    """)
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)

    results = []
    for r in rows:
        results.append(
            {
                "turn_id": str(r["id"]),
                "conversation_id": str(r["conversation_id"]),
                "title": r.get("title") or "",
                "source": r.get("source") or "",
                "model": r.get("model") or "",
                "seq": r.get("seq"),
                "user_query": (r.get("user_turn") or "")[:300],
                "assistant_answer": (r.get("text") or "")[:500],
                "created_at": r.get("created_at", ""),
            }
        )
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


@mcp.tool(name="search_similarity")
async def search_similarity(
    query: str,
    limit: int = 10,
    rerank: bool = True,
    rerank_candidates: int = 50,
    max_tokens: int = 4096,
) -> str:
    """의미 기반 유사도 검색. BM25(키워드) + Dense(벡터) 하이브리드 RRF 융합 + Cross-Encoder 리랭커.

    Stage 1: BM25 (FTS5) + Dense (pgvector ANN) -> RRF fusion
    Stage 2: Cross-encoder reranker (Qwen3-Reranker-4B) on top candidates
    Stage 3: Token-budgeted output

    Args:
        query: 검색할 질문 또는 키워드 (자연어)
        limit: 반환할 결과 수 (기본 10, 최대 30)
        rerank: Cross-encoder reranker 사용 여부 (기본 True)
        rerank_candidates: 리랭커에 전달할 상위 후보 수 (기본 50, 최대 100)
        max_tokens: 출력 결과의 최대 추정 토큰 수 (기본 4096, 최대 8192)
    """
    from lib.search.hybrid import hybrid_search

    limit = max(1, min(limit, 30))
    rerank_candidates = max(10, min(rerank_candidates, 100))
    max_tokens = max(1024, min(max_tokens, 8192))

    result = await asyncio.to_thread(
        hybrid_search,
        query,
        limit,
        rerank=rerank,
        rerank_candidates=rerank_candidates,
    )

    results_list = result.get("results", [])
    meta = result.get("meta", {})

    if results_list:
        trimmed = []
        est_total = len(json.dumps({"results": [], "meta": meta}))
        token_buf = max_tokens - 100

        for r in results_list:
            r_json = json.dumps(r, ensure_ascii=False)
            est_tokens = len(r_json) // 2
            if est_total + est_tokens > token_buf:
                break
            trimmed.append(r)
            est_total += est_tokens

        result["results"] = trimmed
        result["meta"] = meta
        if len(trimmed) < len(results_list):
            meta["truncated"] = len(results_list) - len(trimmed)
            meta["truncated_reason"] = "token_budget"

    return json.dumps(result, ensure_ascii=False)


# ── Phase 1 Tools ─────────────────────────────────────────────


@mcp.tool(name="search_conversations")
async def search_conversations(
    source: Optional[str] = None, model: Optional[str] = None, limit: int = 20, offset: int = 0
) -> str:
    """대화 목록을 검색/조회합니다. source나 model로 필터링 가능.

    Args:
        source: 출처 필터 (예: claude-code, cli, web)
        model: 모델명 필터 (예: claude-sonnet-4.6, deepseek-v4-flash)
        limit: 반환 개수 (기본 20, 최대 100)
        offset: 건너뛸 개수 (페이지네이션)
    """
    limit = max(1, min(limit, 100))
    conditions = []
    if source:
        conditions.append(f"c.source = '{esc_sql(source)}'")
    if model:
        conditions.append(f"c.model = '{esc_sql(model)}'")
    where = " AND ".join(conditions) if conditions else "TRUE"

    rows = await _fetch_json(f"""
        SELECT c.id, c.title, c.source, c.model, c.created_at::text,
               (SELECT COUNT(*) FROM turns t WHERE t.conversation_id = c.id) AS turn_count
        FROM conversations c
        WHERE {where}
        ORDER BY c.created_at DESC
        LIMIT {limit} OFFSET {offset}
    """)
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)

    results = []
    for r in rows:
        results.append(
            {
                "id": str(r["id"]),
                "title": r.get("title") or "",
                "source": r.get("source") or "",
                "model": r.get("model") or "",
                "turn_count": r.get("turn_count", 0),
                "created_at": r.get("created_at", ""),
            }
        )
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


@mcp.tool(name="get_conversation")
async def get_conversation(conversation_id: str) -> str:
    """대화 ID로 전체 대화 스레드를 조회합니다.

    Args:
        conversation_id: 대화 UUID
    """
    conv = await _fetch_json(f"""
        SELECT c.id, c.title, c.source, c.model, c.created_at::text
        FROM conversations c WHERE c.id = '{esc_sql(conversation_id)}'::uuid
    """)
    if not conv:
        return json.dumps({"error": "Conversation not found"}, ensure_ascii=False)

    turns = await _fetch_json(f"""
        SELECT seq, user_turn, text, thinking, agent, meta, pipeline_state, created_at::text
        FROM turns WHERE conversation_id = '{esc_sql(conversation_id)}'::uuid
        ORDER BY seq ASC
    """)

    return json.dumps(
        {
            "conversation": {
                "id": str(conv[0]["id"]),
                "title": conv[0].get("title") or "",
                "source": conv[0].get("source") or "",
                "model": conv[0].get("model") or "",
                "created_at": conv[0].get("created_at", ""),
            },
            "turns": [
                {
                    "seq": t["seq"],
                    "user_turn": (t.get("user_turn") or "")[:500],
                    "text": (t.get("text") or "")[:2000],
                    "thinking": (t.get("thinking") or "")[:500] if t.get("thinking") else None,
                    "agent": t.get("agent"),
                    "pipeline_state": t.get("pipeline_state"),
                }
                for t in turns
            ],
            "turn_count": len(turns),
        },
        ensure_ascii=False,
    )


@mcp.tool(name="search_turns")
async def search_turns(
    keyword: Optional[str] = None,
    agent: Optional[str] = None,
    pipeline_state: Optional[str] = None,
    meta_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """대화 턴(Turn)을 검색합니다. 키워드, agent, 상태 등으로 필터링.

    Args:
        keyword: 검색 키워드 (user_turn, text, thinking 전체 ILIKE 검색)
        agent: agent 필터 (예: claude-code, deepseek-v4-flash)
        pipeline_state: pipeline 상태 필터 (pending, verified 등)
        meta_type: meta type 필터
        limit: 반환 개수 (기본 20, 최대 100)
        offset: 건너뛸 개수 (페이지네이션)
    """
    limit = max(1, min(limit, 100))
    conditions = []
    if keyword:
        kw = esc_sql(keyword)
        conditions.append(
            f"(COALESCE(user_turn,'') || ' ' || COALESCE(text,'') || ' ' || COALESCE(thinking,'')) ILIKE '%{kw}%'"
        )
    if agent:
        conditions.append(f"agent = '{esc_sql(agent)}'")
    if pipeline_state:
        conditions.append(f"pipeline_state = '{esc_sql(pipeline_state)}'")
    if meta_type:
        conditions.append(f"meta->>'type' = '{esc_sql(meta_type)}'")
    where = " AND ".join(conditions) if conditions else "TRUE"

    rows = await _fetch_json(f"""
        SELECT id, conversation_id, seq, user_turn, text, agent,
               pipeline_state, meta, created_at::text, est_chars
        FROM turns
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT {limit} OFFSET {offset}
    """)
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)

    results = []
    for r in rows:
        results.append(
            {
                "id": str(r["id"]),
                "conversation_id": str(r["conversation_id"]),
                "seq": r["seq"],
                "user_turn": (r.get("user_turn") or "")[:300],
                "text": (r.get("text") or "")[:500],
                "agent": r.get("agent"),
                "pipeline_state": r.get("pipeline_state"),
                "est_chars": r.get("est_chars"),
                "created_at": r.get("created_at", ""),
            }
        )
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


@mcp.tool(name="get_turn_facts")
async def get_turn_facts(turn_id: str) -> str:
    """특정 Turn의 fact(추출된 사실) 목록을 조회합니다.

    Args:
        turn_id: Turn UUID
    """
    rows = await _fetch_json(f"""
        SELECT id, turn_id, fact_index, fact_type, evidence, verdict, reason,
               nli_llm, nli_verdict, source, created_at::text
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
        ORDER BY fact_index ASC
    """)
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)

    results = []
    for r in rows:
        results.append(
            {
                "id": str(r["id"]),
                "fact_index": r["fact_index"],
                "fact_type": r.get("fact_type", ""),
                "evidence": (r.get("evidence") or "")[:500],
                "verdict": r.get("verdict", "pending"),
                "nli_verdict": r.get("nli_verdict"),
                "source": r.get("source", ""),
                "created_at": r.get("created_at", ""),
            }
        )
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


@mcp.tool(name="ingest")
async def ingest(conversation_json: str) -> str:
    """대화 + 턴을 일괄 저장합니다. 여러 turn을 한 번에 저장할 때 사용.

    Args:
        conversation_json: JSON 문자열. 형식:
            {"source": "claude-code", "model": "...", "title": "...",
             "turns": [{"user_turn": "...", "text": "...", "thinking": "...", "agent": "..."}]}
    """
    try:
        data = json.loads(conversation_json)
    except (json.JSONDecodeError, TypeError) as e:
        return json.dumps({"error": f"Invalid JSON: {e}"}, ensure_ascii=False)

    source = (data.get("source") or "mcp_ingest")[:50]
    model = (data.get("model") or "")[:100]
    title = (data.get("title") or "MCP Ingest")[:200]
    turns_data = data.get("turns", [])

    if not turns_data:
        return json.dumps({"error": "turns array is required"}, ensure_ascii=False)

    cid = await _fetch_json(
        f"INSERT INTO conversations (title, source, model) VALUES ("
        f"'{esc_sql(title)}', '{esc_sql(source)}', '{esc_sql(model)}') RETURNING id"
    )
    if not cid:
        return json.dumps({"error": "Failed to create conversation"}, ensure_ascii=False)
    conversation_id = cid[0]["id"]

    seq_row = await _fetch_json(
        f"SELECT COALESCE(MAX(seq), 0) AS current_max "
        f"FROM turns WHERE conversation_id = '{esc_sql(conversation_id)}'::uuid"
    )
    seq = (seq_row[0]["current_max"] if seq_row else 0) + 1

    inserted = 0
    for turn in turns_data:
        user_turn = (turn.get("user_turn") or "")[:4000]
        text = (turn.get("text") or "")[:8000]
        thinking = (turn.get("thinking") or "")[:4000]
        agent = (turn.get("agent") or source)[:50]

        ok = await _execute(
            f"INSERT INTO turns (conversation_id, seq, user_turn, text, thinking, agent) "
            f"VALUES ('{esc_sql(conversation_id)}'::uuid, {seq}, "
            f"'{esc_sql(user_turn)}', '{esc_sql(text)}', "
            f"'{esc_sql(thinking)}', '{esc_sql(agent)}')"
        )
        if ok:
            inserted += 1
            seq += 1

    return json.dumps(
        {
            "status": "saved",
            "conversation_id": str(conversation_id),
            "turns_inserted": inserted,
        },
        ensure_ascii=False,
    )


# ── Telegram tools ─────────────────────────────────────────────


@mcp.tool(name="telegram_send")
async def telegram_send(text: str) -> str:
    """텔레그램으로 메시지를 전송합니다. 파이프라인 알림, 상태 보고 등에 사용.

    Args:
        text: 전송할 메시지 내용
    """
    from lib.notify import Notifier

    _sf = Path.home() / ".config/devforge/secrets.env"
    if _sf.exists():
        _s = {}
        for _line in _sf.read_text().split("\n"):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                _s[_k.strip()] = _v.strip().strip('"').strip("'")
        ok = await asyncio.to_thread(Notifier(_s).send_telegram, text)
    else:
        ok = False
    return json.dumps({"ok": ok}, ensure_ascii=False)


@mcp.tool(name="fact_list_pending")
async def fact_list_pending(limit: int = 10) -> str:
    """NEUTRAL 판정을 받고 아직 사용자 검토가 필요한 fact 목록을 조회합니다.

    Args:
        limit: 반환할 개수 (기본 10, 최대 50)
    """
    limit = max(1, min(limit, 50))
    rows = await _fetch_json(
        f"SELECT id::text, left(evidence, 300) AS evidence, fact_type, "
        f"  turn_id::text, "
        f"  to_char(created_at AT TIME ZONE 'Asia/Seoul', 'MM/DD HH24:MI') AS kst "
        f"FROM review_facts "
        f"WHERE nli_llm='NEUTRAL' AND user_verdict IS NULL "
        f"ORDER BY created_at DESC LIMIT {limit}"
    )
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)
    return json.dumps({"count": len(rows), "results": rows}, ensure_ascii=False)


@mcp.tool(name="fact_confirm")
async def fact_confirm(fact_id: str) -> str:
    """NEUTRAL fact를 GROUNDED로 확정합니다. user_verdict='GROUNDED'로 설정.

    Args:
        fact_id: review_fact UUID
    """
    ok = await _execute(
        f"UPDATE review_facts SET user_verdict = 'GROUNDED' WHERE id = '{esc_sql(fact_id)}'::uuid"
    )
    return json.dumps({"ok": ok, "fact_id": fact_id, "verdict": "GROUNDED"}, ensure_ascii=False)


@mcp.tool(name="fact_reject")
async def fact_reject(fact_id: str) -> str:
    """NEUTRAL fact를 UNGROUNDED로 기각합니다. user_verdict='UNGROUNDED'로 설정.

    Args:
        fact_id: review_fact UUID
    """
    ok = await _execute(
        f"UPDATE review_facts SET user_verdict = 'UNGROUNDED' WHERE id = '{esc_sql(fact_id)}'::uuid"
    )
    return json.dumps({"ok": ok, "fact_id": fact_id, "verdict": "UNGROUNDED"}, ensure_ascii=False)


@mcp.tool(name="obs_search")
async def obs_search(
    category: Optional[str] = None,
    source: Optional[str] = None,
    tags: Optional[str] = None,
    query: Optional[str] = None,
    limit: int = 10,
) -> str:
    """observations 테이블 검색. PostToolUse 훅 자동 기록 + obs_write 수동 기록을 통합 조회.

    Args:
        category: 카테고리 필터 (insight, decision, test_result, edit, error, reasoning, reference 등)
        source: 출처 필터 (생략 시 전체 - hook:PostToolUse, mcp:obs_write, cli:obs, 등)
        tags: JSON 태그 필터 - {"domain": ["mcp"]} 형식 (object-of-arrays JSON 문자열)
        query: observation 텍스트 부분 검색 (pg_trgm ILIKE)
        limit: 반환 개수 (기본 10, 최대 200)
    """
    limit = max(1, min(limit, 200))
    conds = []
    if source:
        conds.append(f"source = '{esc_sql(source)}'")
    if category:
        conds.append(f"category = '{esc_sql(category)}'")
    if tags:
        try:
            tags_obj = json.loads(tags)
            conds.append(f"tags @> $JSON${json.dumps(tags_obj, ensure_ascii=False)}$JSON$::jsonb")
        except json.JSONDecodeError:
            pass
    if query:
        q = esc_sql(query)
        conds.append(f"observation ILIKE '%{q}%'")
    where = " AND ".join(conds) if conds else "TRUE"

    rows = await _fetch_json(f"""
        SELECT id::text, observation, category, source, context::text, tags::text, created_at::text
        FROM observations
        WHERE {where}
        ORDER BY created_at DESC
        LIMIT {limit}
    """)
    if not rows:
        return json.dumps({"count": 0, "results": []}, ensure_ascii=False)

    results = []
    for r in rows:
        ctx = {}
        try:
            ctx = json.loads(r.get("context") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        tag_obj = {}
        try:
            tag_obj = json.loads(r.get("tags") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
        results.append(
            {
                "id": r["id"],
                "observation": (r.get("observation") or "")[:300],
                "category": r.get("category", ""),
                "source": r.get("source", ""),
                "tags": tag_obj,
                "tool": ctx.get("tool", ""),
                "exit_code": ctx.get("exit_code"),
                "file_path": ctx.get("file_path"),
                "created_at": r.get("created_at", ""),
            }
        )
    return json.dumps({"count": len(results), "results": results}, ensure_ascii=False)


@mcp.tool(name="obs_write")
async def obs_write(
    observation: str,
    category: str = "general",
    tags: Optional[str] = None,
    context_json: Optional[str] = None,
) -> str:
    """관찰 기록 (수동). 세션 내 어디서든 호출 가능.

    MCP 검색 결과 요약, Deep Dive 분석 과정, DB 분석 결과, 설계 결정 등을 기록.
    PostToolUse hook이 자동 기록하는 test_result/edit/error 외의 모든 용도.

    Args:
        observation: 관찰 내용
        category: 카테고리 (insight, decision, reasoning, reference, db_result, config, general, 등)
        tags: {"domain": ["mcp","search"], "tier": ["reference"]} 형식 JSON 문자열
        context_json: 추가 컨텍스트 JSON 문자열 (선택)
    """
    from lib.observation import observe as _observe

    tag_obj = None
    if tags:
        try:
            tag_obj = json.loads(tags)
        except json.JSONDecodeError:
            pass
    ctx_obj = None
    if context_json:
        try:
            ctx_obj = json.loads(context_json)
        except json.JSONDecodeError:
            pass

    oid = _observe(
        observation, category=category, source="mcp:obs_write", context=ctx_obj, tags=tag_obj
    )
    if oid:
        return json.dumps({"ok": True, "id": oid}, ensure_ascii=False)
    return json.dumps(
        {"ok": False, "error": "observation empty or insert failed"}, ensure_ascii=False
    )


@mcp.tool(name="obs_remediate")
async def obs_remediate(
    observation: str,
    category: str = "error",
    tags_json: str = "",
    source: str = "",
) -> str:
    """Pattern 2+4: Match observation against reflex rules. Auto-fix>=0.7, notify>=0.3, log unknown.

    Args:
        observation: 관찰 내용
        category: 카테고리 (기본 error)
        tags_json: 태그 JSON 문자열 (선택)
        source: 관찰 출처 (선택, trigger_source 필터에 사용)
    """
    from lib.auto_fix import remediate_observation

    tags = None
    if tags_json:
        try:
            tags = json.loads(tags_json)
        except json.JSONDecodeError:
            pass
    result = remediate_observation(observation, category=category, tags=tags, source=source or None)
    return json.dumps(result, ensure_ascii=False, default=str)


@mcp.tool(name="action_write")
async def action_write(
    instruction: str,
    action_type: str = "systemctl",
    action_params_json: str = "",
    priority: str = "P1_CONTEXT",
) -> str:
    """액션을 큐에 등록합니다. Watchdog이 비동기적으로 실행합니다.

    Claude Code는 결정만 내리고, 실제 실행은 Watchdog이 담당합니다.
    실행 전에 action_type에 해당하는 검증을 Watchdog이 수행합니다.

    Args:
        instruction: 실행할 명령에 대한 자연어 설명
        action_type: 실행 유형 (systemctl, podman, cli)
        action_params_json: 실행 파라미터 JSON 문자열
            systemctl: {"service": "devforge-inference", "command": "restart"}
            podman: {"container": "devforge-inference", "command": "restart"}
            cli: {"script": "cli.py", "args": ["task", "update", "..."]}
        priority: P0_HOT_FIX (즉시), P1_CONTEXT (일반), P2_LOW (여유)
    """
    from lib.action_queue import action_write as _action_write

    params = None
    if action_params_json:
        try:
            params = json.loads(action_params_json)
        except json.JSONDecodeError:
            return json.dumps(
                {"ok": False, "error": "Invalid action_params_json"}, ensure_ascii=False
            )

    pid = _action_write(
        instruction=instruction,
        priority=priority,
        action_type=action_type,
        action_params=params,
    )
    if pid:
        return json.dumps({"ok": True, "pulse_id": pid}, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "Failed to write action"}, ensure_ascii=False)


@mcp.tool(name="action_poll_results")
async def action_poll_results(pulse_id: str) -> str:
    """액션 실행 결과를 조회합니다. Watchdog이 실행한 후 observation에 기록됩니다."""
    from lib.action_queue import action_poll_results as _poll

    results = _poll(pulse_id)
    return json.dumps({"pulse_id": pulse_id, "results": results}, ensure_ascii=False, default=str)


@mcp.tool(name="deepdive_verify_sandbox")
async def deepdive_verify_sandbox(
    project_dir: str,
    test_cmd: str,
    affected_files: Optional[str] = None,
) -> str:
    """Deep Dive 7단계 검증을 podman 샌드박스에서 격리 실행합니다.

    LLM이 수정한 코드가 host 파일시스템에 직접 영향을 주지 않도록,
    프로젝트 디렉토리를 읽기 전용(-v :ro)으로 non-root(nobody)로 마운트하고
    --network none으로 실행합니다. action_queue(sandbox_verify)를 통해
    Watchdog이 비동기 실행하고, action_poll_results로 결과를 폴링합니다.

    주의: SANDBOX_IMAGE(python:3.12-alpine)에는 pytest가 없고 --network none이라
    설치도 불가하므로, 1차 구현에서 test_cmd는 stdlib unittest만 지원한다
    (예: "python -m unittest discover -s ."). project_dir은 반드시
    SANDBOX_VERIFY_ALLOWED_ROOT(/opt/projects/server) 하위여야 하며,
    그 외 경로는 host 시크릿 유출 방지를 위해 거부된다.

    Args:
        project_dir: 검증할 프로젝트 디렉토리 (절대 경로, /opt/projects/server 하위만 허용)
        test_cmd: 샌드박스 내 실행할 테스트 명령 (예: "python -m unittest discover -s .")
        affected_files: 변경된 파일 목록 (쉼표 구분) — .md만 있으면 샌드박스 생략
    """
    import os

    from lib.action_queue import action_write as _action_write
    from lib.watchdog.config import SANDBOX_VERIFY_ALLOWED_ROOT

    if not project_dir or not test_cmd:
        return json.dumps(
            {"ok": False, "error": "project_dir and test_cmd are required"}, ensure_ascii=False
        )
    if any(c in test_cmd for c in (";", "|", "&", "$", "`", "\n")):
        return json.dumps({"ok": False, "error": "Invalid test_cmd"}, ensure_ascii=False)
    real_root = os.path.realpath(SANDBOX_VERIFY_ALLOWED_ROOT)
    real_dir = os.path.realpath(project_dir)
    if real_dir != real_root and not real_dir.startswith(real_root + os.sep):
        return json.dumps(
            {"ok": False, "error": f"project_dir must be under {SANDBOX_VERIFY_ALLOWED_ROOT}"},
            ensure_ascii=False,
        )

    if affected_files:
        files = [f.strip() for f in affected_files.split(",") if f.strip()]
        if files and all(f.endswith(".md") for f in files):
            return json.dumps(
                {
                    "ok": False,
                    "skipped": True,
                    "reason": "doc-only changes — sandbox not required",
                    "pulse_id": "",
                },
                ensure_ascii=False,
            )

    pid = _action_write(
        instruction=f"Deep Dive sandbox verify: {project_dir} — {test_cmd}",
        priority="P1_CONTEXT",
        action_type="sandbox_verify",
        action_params={"project_dir": project_dir, "test_cmd": test_cmd},
    )
    if pid:
        return json.dumps({"ok": True, "pulse_id": pid}, ensure_ascii=False)
    return json.dumps({"ok": False, "error": "Failed to queue sandbox_verify"}, ensure_ascii=False)


# ── Aider sequential review tool ─────────────────────────────


@mcp.tool(name="review_sequential")
async def review_sequential(task: str, paths: str) -> str:
    """Aider 작업을 위한 순차 코드 리뷰. 지정된 파일들을 하나씩 읽어 구조를 분석합니다.

    각 파일의 Status 헤더 -> 구조(imports, functions, classes) -> 작업 연관성을
    순차적으로 평가하여 반환합니다.

    Args:
        task: 수행할 코딩 작업 설명 (자연어)
        paths: 리뷰할 파일 경로들 (쉼표 구분, /scripts/ 기준 상대 경로)
    """
    import re

    file_paths = [p.strip() for p in paths.split(",") if p.strip()]
    if not file_paths:
        return json.dumps({"error": "paths is required (comma-separated)"}, ensure_ascii=False)

    reviews = []
    for fp in file_paths:
        full = Path(SCRIPTS_DIR) / fp if not fp.startswith("/") else Path(fp)
        if not full.exists():
            reviews.append({"path": fp, "error": "file not found", "resolved": str(full)})
            continue

        text = full.read_text()
        lines = text.split("\n")
        total = len(lines)

        status = "unknown"
        caller = "unknown"
        docstring = ""
        in_doc = False
        doc_parts = []
        for line in lines[:20]:
            if line.startswith("# Status:"):
                status = line.replace("# Status:", "").strip()
            elif line.startswith("# Path:"):
                caller = line.replace("# Path:", "").strip()
            elif line.strip().startswith(('"""', "'''")):
                in_doc = not in_doc
                if not in_doc:
                    break
            elif in_doc:
                doc_parts.append(line.strip())
        if doc_parts:
            docstring = " ".join(doc_parts)[:300]

        imports = []
        funcs = []
        classes = []
        constants = {}
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith(("import ", "from ")):
                imports.append(s)
            elif re.match(r"^(?:async\s+)?def\s+\w+\s*\(", s):
                m = re.match(r"^(?:async\s+)?def\s+(\w+)\s*\((.*?)\)\s*(?:->\s*(.*?))?\s*:", s)
                if m:
                    name = m.group(1)
                    params = m.group(2)[:80]
                    returns = (m.group(3) or "").strip()[:40]
                    sig = f"{name}({params})"
                    if returns:
                        sig += f" -> {returns}"
                    funcs.append(sig)
            elif s.startswith("class ") and s.endswith(":"):
                m = re.match(r"class\s+(\w+)(\(.*?\))?\s*:", s)
                if m:
                    classes.append(m.group(1) + (m.group(2) or ""))
            elif re.match(r"^[A-Z][A-Z_0-9]+\s*=", s):
                k, _, v = s.partition("=")
                constants[k.strip()] = v.strip()[:60]

        head = [l.rstrip() for l in lines[:8]]
        tail = [l.rstrip() for l in lines[-6:]] if total > 12 else []

        reviews.append(
            {
                "path": fp,
                "status": status,
                "caller": caller,
                "lines": total,
                "docstring": docstring,
                "imports": imports[:15],
                "import_count": len(imports),
                "funcs": funcs[:20],
                "func_count": len(funcs),
                "classes": classes[:10],
                "constants": constants,
                "head": head,
                "tail": tail,
            }
        )

    return json.dumps(
        {
            "task": task,
            "file_count": len(reviews),
            "reviews": reviews,
        },
        ensure_ascii=False,
        indent=2,
    )


# ── Deep Dive 단계 heartbeat (Phase 1 + Phase 2) ──────────────
# 대화형 Deep Dive 세션 hang 감지용. 단계별 base timeout + min/max bound로
# 만료 판정, 1·2회 초과는 경고만, 3회 연속 초과 시 세션 ABORTED + Slack.
# Phase 2: affected_files(LSP blast_radius)가 주어지면 base+파일당마진 비례로
# max_bound를 동적 재계산(min/max로 clamp). 미지정 시 Phase 1과 동일하게 정적 max 사용.

DEEPDIVE_STEP_BUDGETS = {
    1: {"name": "yggdrasil_planning", "base": 180, "min": 60, "max": 600},
    2: {"name": "code_explore", "base": 300, "min": 120, "max": 900},
    3: {"name": "lsp_analysis", "base": 300, "min": 120, "max": 1200},
    4: {"name": "external_verify", "base": 300, "min": 120, "max": 900},
    5: {"name": "plan_finalize", "base": 240, "min": 60, "max": 600},
    6: {"name": "implementation", "base": 600, "min": 300, "max": 2400},
    7: {"name": "verification", "base": 300, "min": 120, "max": 900},
}
DEEPDIVE_CHECK_INTERVAL = 60
DEEPDIVE_OVERRUN_LIMIT = 3
DEEPDIVE_FILE_MARGIN_SEC = 120  # 영향 파일 1개당 추가 마진 (blast_radius 비례 연장)


def _deepdive_effective_max(step: int, affected_files: Optional[int]) -> int:
    """Phase 2 동적 max_bound 계산.

    affected_files가 None이면 Phase 1 정적 max를 그대로 사용(하위호환).
    지정되면 base + affected_files*DEEPDIVE_FILE_MARGIN_SEC을 min/max로 clamp.
    음수 등 비정상값은 0으로 취급.
    """
    budget = DEEPDIVE_STEP_BUDGETS[step]
    if affected_files is None:
        return budget["max"]
    n = max(0, affected_files)
    raw = budget["base"] + n * DEEPDIVE_FILE_MARGIN_SEC
    return min(max(raw, budget["min"]), budget["max"])


# ── Deep Dive 단계 heartbeat 툴 ───────────────────────────────


@mcp.tool(name="deepdive_step_enter")
async def deepdive_step_enter(
    session_id: str,
    step: int,
    step_name: str = "",
    force: bool = False,
    affected_files: Optional[int] = None,
) -> str:
    """Deep Dive 단계 진입을 기록합니다. 세션 시작 시 각 단계 진입마다 호출.

    hang 판정용 base/min/max bound는 DEEPDIVE_STEP_BUDGETS에서 단계별로 적용.
    기존 ACTIVE 행이 있으면 overrun_count를 0으로 리셋하고 재시작.
    기존 행이 ABORTED 상태면 재진입을 거부한다(circuit breaker 무력화 방지).
    force=True를 명시해야만 ABORTED 상태를 초기화하고 재시작할 수 있다.

    Phase 2: affected_files(LSP blast_radius로 파악한 영향 파일 수)를 넘기면
    max_bound를 base + affected_files*DEEPDIVE_FILE_MARGIN_SEC로 동적 재계산해
    min/max bound 사이로 clamp한다. 생략(None)하면 Phase 1과 동일하게 정적
    max_bound_sec을 사용한다(하위호환).

    Args:
        session_id: Deep Dive 세션 식별자 (예: conversation UUID)
        step: Deep Dive 단계 번호 (1~7)
        step_name: 단계 이름 (선택, 미지정 시 DEEPDIVE_STEP_BUDGETS 이름 사용)
        force: True면 ABORTED 상태여도 강제로 리셋 후 재진입 (기본 False)
        affected_files: LSP blast_radius 영향 파일 수 (선택, 생략 시 정적 max 사용)
    """
    if step not in DEEPDIVE_STEP_BUDGETS:
        return json.dumps({"ok": False, "error": f"step {step} not in 1..7"}, ensure_ascii=False)
    budget = DEEPDIVE_STEP_BUDGETS[step]
    name = step_name.strip() or budget["name"]
    effective_max = _deepdive_effective_max(step, affected_files)

    if not force:
        existing = await _fetch_json(
            f"SELECT status FROM deepdive_steps "
            f"WHERE session_id = '{esc_sql(session_id[:200])}' AND step = {step}"
        )
        if existing and existing[0]["status"] == "ABORTED":
            return json.dumps(
                {
                    "ok": False,
                    "error": (
                        "step previously ABORTED (반복 hang으로 자동 중단됨) — "
                        "재시도하려면 원인을 먼저 확인하고 force=true로 재진입하세요"
                    ),
                    "session_id": session_id,
                    "step": step,
                },
                ensure_ascii=False,
            )

    affected_files_sql = "NULL" if affected_files is None else str(max(0, affected_files))
    ok = await _execute(
        f"INSERT INTO deepdive_steps "
        f"(session_id, step, step_name, base_timeout_sec, min_bound_sec, max_bound_sec, "
        f"status, started_at, ended_at, elapsed_sec, overrun_count, last_heartbeat_at, affected_files) "
        f"VALUES ('{esc_sql(session_id[:200])}', {step}, '{esc_sql(name[:100])}', "
        f"{budget['base']}, {budget['min']}, {effective_max}, 'ACTIVE', NOW(), NULL, NULL, 0, NOW(), "
        f"{affected_files_sql}) "
        f"ON CONFLICT (session_id, step) DO UPDATE SET "
        f"step_name = EXCLUDED.step_name, "
        f"base_timeout_sec = EXCLUDED.base_timeout_sec, "
        f"min_bound_sec = EXCLUDED.min_bound_sec, "
        f"max_bound_sec = EXCLUDED.max_bound_sec, "
        f"status = 'ACTIVE', started_at = NOW(), ended_at = NULL, "
        f"elapsed_sec = NULL, overrun_count = 0, last_heartbeat_at = NOW(), "
        f"affected_files = EXCLUDED.affected_files"
    )
    return json.dumps(
        {
            "ok": ok,
            "session_id": session_id,
            "step": step,
            "step_name": name,
            "effective_max_sec": effective_max,
            "affected_files": affected_files,
        },
        ensure_ascii=False,
    )


@mcp.tool(name="deepdive_step_exit")
async def deepdive_step_exit(session_id: str, step: int) -> str:
    """Deep Dive 단계 종료를 기록합니다. 단계 완료 시 호출.

    ended_at/elapsed_sec 기록, status=DONE, overrun_count=0 리셋.
    elapsed_sec는 Phase 2 percentile 재교정용 실측 데이터.

    Args:
        session_id: Deep Dive 세션 식별자
        step: Deep Dive 단계 번호 (1~7)
    """
    if step not in DEEPDIVE_STEP_BUDGETS:
        return json.dumps({"ok": False, "error": f"step {step} not in 1..7"}, ensure_ascii=False)

    ok = await _execute(
        f"UPDATE deepdive_steps SET "
        f"ended_at = NOW(), "
        f"elapsed_sec = EXTRACT(EPOCH FROM (NOW() - started_at))::int, "
        f"status = 'DONE', overrun_count = 0 "
        f"WHERE session_id = '{esc_sql(session_id[:200])}' AND step = {step} "
        f"AND status = 'ACTIVE'"
    )
    row = await _fetch_json(
        f"SELECT elapsed_sec FROM deepdive_steps "
        f"WHERE session_id = '{esc_sql(session_id[:200])}' AND step = {step}"
    )
    elapsed = row[0]["elapsed_sec"] if row else None
    return json.dumps({"ok": ok, "elapsed_sec": elapsed}, ensure_ascii=False)


@mcp.tool(name="deepdive_session_heartbeat")
async def deepdive_session_heartbeat(session_id: str, step: int) -> str:
    """Deep Dive 단계의 생존 신호를 갱신합니다. long-running 단계에서 주기 호출.

    false positive 방지용 — started_at은 유지하고 last_heartbeat_at만 갱신.
    _deepdive_check_expired는 last_heartbeat_at 기준 staleness(base_timeout_sec 초과 시
    dead man's switch 발동)와 started_at 기준 max_bound_sec(heartbeat와 무관한 절대
    상한선)를 둘 다 검사한다. 즉 heartbeat를 주기적으로 호출하면 staleness 판정은
    피할 수 있지만, max_bound_sec 절대 상한은 heartbeat로도 넘길 수 없다.

    Args:
        session_id: Deep Dive 세션 식별자
        step: Deep Dive 단계 번호 (1~7)
    """
    ok = await _execute(
        f"UPDATE deepdive_steps SET last_heartbeat_at = NOW() "
        f"WHERE session_id = '{esc_sql(session_id[:200])}' AND step = {step} "
        f"AND status = 'ACTIVE'"
    )
    return json.dumps({"ok": ok, "session_id": session_id, "step": step}, ensure_ascii=False)


@mcp.tool(name="deepdive_session_status")
async def deepdive_session_status(session_id: str) -> str:
    """Deep Dive 세션의 전체 단계 상태를 조회합니다.

    인터랙티브 에이전트가 다음 단계로 넘어가기 전, 혹은 재시도 전에
    직전 단계가 ABORTED되지 않았는지 확인하는 용도. Slack 알림을 놓쳤거나
    에이전트가 직접 상태를 확인해야 할 때 사용.

    Args:
        session_id: Deep Dive 세션 식별자
    """
    rows = await _fetch_json(
        f"SELECT step, step_name, status, overrun_count, max_bound_sec, affected_files, "
        f"EXTRACT(EPOCH FROM (NOW() - started_at))::int AS age_sec, elapsed_sec "
        f"FROM deepdive_steps WHERE session_id = '{esc_sql(session_id[:200])}' "
        f"ORDER BY step"
    )
    aborted = [r["step"] for r in rows if r["status"] == "ABORTED"]
    return json.dumps(
        {
            "ok": True,
            "session_id": session_id,
            "steps": rows,
            "has_aborted_step": bool(aborted),
            "aborted_steps": aborted,
        },
        ensure_ascii=False,
    )


def _deepdive_send_alert(text: str) -> bool:
    """Send Slack alert for Deep Dive hang via lib.notify. Returns success."""
    from lib.notify import Notifier

    _sf = Path.home() / ".config/devforge/secrets.env"
    if not _sf.exists():
        return False
    _s = {}
    for _line in _sf.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _s[_k.strip()] = _v.strip().strip('"').strip("'")
    channel = _s.get("SLACK_CHANNEL", "#alerts")
    try:
        return Notifier(_s).send_slack(channel, text)
    except Exception:
        return False


async def _deepdive_check_expired() -> list[dict]:
    """Scan ACTIVE deepdive_steps that are overrun.

    두 가지 독립적인 만료 사유를 모두 검사한다:
      - max_bound_exceeded: started_at 기준 max_bound_sec 초과 — heartbeat와
        무관한 절대 상한선(안전망).
      - heartbeat_stale: base_timeout_sec은 지났는데 last_heartbeat_at도
        base_timeout_sec 이상 갱신이 없음 — 진짜 dead man's switch 판정.
        heartbeat를 주기적으로 호출하면 이 사유로는 걸리지 않는다.
    """
    rows = await _fetch_json(
        f"SELECT session_id, step, step_name, base_timeout_sec, max_bound_sec, overrun_count, "
        f"EXTRACT(EPOCH FROM (NOW() - started_at))::int AS age_sec, "
        f"EXTRACT(EPOCH FROM (NOW() - last_heartbeat_at))::int AS heartbeat_stale_sec, "
        f"CASE WHEN (NOW() - started_at) > (max_bound_sec || ' seconds')::interval "
        f"THEN 'max_bound_exceeded' ELSE 'heartbeat_stale' END AS reason "
        f"FROM deepdive_steps WHERE status = 'ACTIVE' "
        f"AND ("
        f"  (NOW() - started_at) > (max_bound_sec || ' seconds')::interval "
        f"  OR ("
        f"    (NOW() - started_at) > (base_timeout_sec || ' seconds')::interval "
        f"    AND (NOW() - last_heartbeat_at) > (base_timeout_sec || ' seconds')::interval"
        f"  )"
        f")"
    )
    return rows


async def _deepdive_expiry_loop() -> None:
    """Background loop: every 60s escalate ACTIVE steps that are overrun.

    1·2회 초과 → Slack 경고만. 3회 연속 → status=ABORTED + Slack 에스컬레이션.
    """
    while True:
        try:
            overrun_rows = await _deepdive_check_expired()
            for r in overrun_rows:
                sid, step, name = r["session_id"], r["step"], r["step_name"]
                reason = r["reason"]
                overrun = r["overrun_count"] + 1
                await _execute(
                    f"UPDATE deepdive_steps SET overrun_count = {overrun}, "
                    f"last_heartbeat_at = last_heartbeat_at "
                    f"WHERE session_id = '{esc_sql(sid)}' AND step = {step} AND status = 'ACTIVE'"
                )
                if overrun >= DEEPDIVE_OVERRUN_LIMIT:
                    await _execute(
                        f"UPDATE deepdive_steps SET status = 'ABORTED' "
                        f"WHERE session_id = '{esc_sql(sid)}' AND step = {step}"
                    )
                    await asyncio.to_thread(
                        _deepdive_send_alert,
                        f"[DeepDive] {sid} step {step}({name}) "
                        f"{overrun}회 연속 초과({reason}) — 세션 자동 중단 (ABORTED)",
                    )
                else:
                    await asyncio.to_thread(
                        _deepdive_send_alert,
                        f"[DeepDive] {sid} step {step}({name}) {overrun}회차 "
                        f"초과({reason}) — 주의 (자동 중단은 3회부터)",
                    )
        except Exception as e:
            logger.warning("deepdive expiry loop error: %s", e)
        await asyncio.sleep(DEEPDIVE_CHECK_INTERVAL)


# ── ASGI app factory ─────────────────────────────────────────


def _create_app():
    """Create the FastMCP ASGI app with /health endpoint + deepdive expiry loop."""
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    mcp_app = mcp.http_app(path="/")
    inner_lifespan = mcp_app.lifespan

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(_deepdive_expiry_loop())
        try:
            async with inner_lifespan(app):
                yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    root = Starlette(
        routes=[
            Mount("/mcp", app=mcp_app),
            Route("/health", health_endpoint, methods=["GET"]),
        ],
        lifespan=lifespan,
    )
    return root


async def health_endpoint(request):
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok", "server": "devforge-mcp"})


# ── Entry point ─────────────────────────────────────────────


def main():
    """Start the MCP server with uvicorn."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="DevForge MCP Server")
    parser.add_argument("--port", "-p", type=int, default=8000, help="Port (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host (default: 127.0.0.1)")
    args = parser.parse_args()

    logger.info("Starting DevForge MCP on %s:%d", args.host, args.port)
    uvicorn.run(
        "mcp_server:_create_app().root" if False else _create_app(),
        host=args.host,
        port=args.port,
        log_level="info",
    )


# ── FlareSolverr Cloudflare Bypass ───────────────────────────

import re as _re

_FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
_URL_PATTERN = _re.compile(r"^https?://", _re.IGNORECASE)


@mcp.tool(name="flaresolverr_bypass")
async def flaresolverr_bypass(
    url: str,
    session: Optional[str] = None,
    timeout: int = 60000,
    create_session: bool = False,
    destroy_session: bool = False,
    return_only_cookies: bool = False,
    max_response_len: int = 50000,
    rate_limit: bool = True,
) -> str:
    """Cloudflare 보호 페이지 우회하여 HTML/쿠키 반환.

    Args:
        url: 대상 URL
        session: 기존 세션 ID (선택). 없으면 일회용 브라우저 사용
        timeout: 최대 대기 시간(ms, 기본 60000, 최대 300000)
        create_session: True면 세션 생성 후 반환 (url 무시됨)
        destroy_session: True면 세션 파괴 (url, session 필요)
        return_only_cookies: True면 쿠키와 상태만 반환 (HTML 제외)
        max_response_len: HTML 응답 최대 길이 (기본 50000자, 0=무제한)
        rate_limit: True면 8분 간격 속도 제한 적용 (기본 True)
    """
    import httpx

    if rate_limit and not create_session and not destroy_session and url:
        from lib.rate_limiter import wait_if_needed, record_request
        wait_sec = wait_if_needed(url)
        if wait_sec > 0:
            import asyncio
            await asyncio.sleep(wait_sec)

    if create_session and destroy_session:
        return json.dumps(
            {"error": "create_session과 destroy_session은 동시에 사용할 수 없습니다"},
            ensure_ascii=False,
        )

    if not create_session and not destroy_session and not url:
        return json.dumps({"error": "url required for request"}, ensure_ascii=False)

    if not create_session and not destroy_session and not _URL_PATTERN.match(url):
        return json.dumps(
            {"error": f"잘못된 URL 형식: {url} (http:// 또는 https:// 필요)"},
            ensure_ascii=False,
        )

    if destroy_session and not session:
        return json.dumps({"error": "session required for destroy"}, ensure_ascii=False)

    timeout = max(1000, min(timeout, 300000))

    if create_session:
        payload = {"cmd": "sessions.create"}
        if session:
            payload["session"] = session
    elif destroy_session:
        payload = {"cmd": "sessions.destroy", "session": session}
    else:
        payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": timeout,
            "returnOnlyCookies": return_only_cookies,
        }
        if session:
            payload["session"] = session

    async with httpx.AsyncClient(timeout=timeout / 1000 + 30) as client:
        try:
            resp = await client.post(
                _FLARESOLVERR_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.TimeoutException:
            return json.dumps(
                {"error": f"FlareSolverr timeout after {timeout}ms", "status": "timeout"},
                ensure_ascii=False,
            )
        except httpx.HTTPStatusError as e:
            return json.dumps(
                {"error": f"FlareSolverr HTTP {e.response.status_code}: {e.response.text[:200]}", "status": "http_error"},
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps(
                {"error": f"FlareSolverr error: {type(e).__name__}: {e}", "status": "connection_error"},
                ensure_ascii=False,
            )

    if data.get("status") != "ok":
        return json.dumps(
            {"error": data.get("message", "FlareSolverr returned non-ok status"), "status": "flare_error", "raw": data},
            ensure_ascii=False,
        )

    if create_session:
        return json.dumps(
            {"status": "ok", "session": data.get("session"), "message": data.get("message")},
            ensure_ascii=False,
        )

    if destroy_session:
        return json.dumps({"status": "ok", "message": data.get("message")}, ensure_ascii=False)

    sol = data.get("solution", {})
    response_text = sol.get("response", "")
    if max_response_len > 0 and len(response_text) > max_response_len:
        response_text = response_text[:max_response_len] + f"\n... (truncated at {max_response_len} chars)"

    return json.dumps(
        {
            "status": "ok",
            "http_status": sol.get("status"),
            "url": sol.get("url"),
            "response": response_text if not return_only_cookies else None,
            "cookies": sol.get("cookies", []),
            "headers": sol.get("headers", {}),
            "userAgent": sol.get("userAgent"),
            "turnstile_token": sol.get("turnstile_token"),
        },
        ensure_ascii=False,
    )

if __name__ == "__main__":
    main()
