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

import json
import logging
import os
import sys
from typing import Optional

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

import httpx
from lib.db import esc_sql, psql_json, psql_ok
from mcp.server.fastmcp import FastMCP

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
    import asyncio

    return await asyncio.to_thread(psql_json, sql)


async def _execute(sql: str) -> bool:
    import asyncio

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
            {"error": "Embedder unavailable (try again when Pod B has embedder loaded)"},
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

    Stage 1: BM25 (FTS5) + Dense (pgvector ANN) → RRF fusion
    Stage 2: Cross-encoder reranker (Qwen3-Reranker-4B) on top candidates
    Stage 3: Token-budgeted output — 결과 총 토큰이 max_tokens를 넘지 않도록 자동 조정

    각 결과에는 대화 메타데이터(conv_title, conv_source, conv_model)와
    rerank_score가 포함되어 LLM이 최종 판단에 활용할 수 있습니다.

    Args:
        query: 검색할 질문 또는 키워드 (자연어)
        limit: 반환할 결과 수 (기본 10, 최대 30)
        rerank: Cross-encoder reranker 사용 여부 (기본 True)
        rerank_candidates: 리랭커에 전달할 상위 후보 수 (기본 50, 최대 100)
        max_tokens: 출력 결과의 최대 추정 토큰 수 (기본 4096, 최대 8192)
    """
    import asyncio

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

    # --- Token budget enforcement ---
    results_list = result.get("results", [])
    meta = result.get("meta", {})

    if results_list:
        trimmed = []
        est_total = len(json.dumps({"results": [], "meta": meta}))  # base overhead
        token_buf = max_tokens - 100  # safety margin

        for r in results_list:
            # Estimate tokens for this result: JSON string / 2 (conservative for mixed Korean/English)
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
    import asyncio

    from telegram_send import send_text

    ok = await asyncio.to_thread(send_text, text)
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
        source: 출처 필터 (생략 시 전체 — hook:PostToolUse, mcp:obs_write, cli:obs, 등)
        tags: JSON 태그 필터 — {"domain": ["mcp"]} 형식 (object-of-arrays JSON 문자열)
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
async def obs_remediate(observation: str, category: str = "error", tags_json: str = "") -> str:
    """Pattern 2+4: Match observation against reflex rules. Auto-fix>=0.7, notify>=0.3, log unknown."""
    from lib.auto_fix import remediate_observation

    tags = None
    if tags_json:
        try:
            tags = json.loads(tags_json)
        except json.JSONDecodeError:
            pass
    result = remediate_observation(observation, category=category, tags=tags)
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
            systemctl: {"service": "devforge-pod-b", "command": "restart"}
            podman: {"container": "devforge-pod-b", "command": "restart"}
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


# ── Aider sequential review tool ─────────────────────────────


@mcp.tool(name="review_sequential")
async def review_sequential(task: str, paths: str) -> str:
    """Aider 작업을 위한 순차 코드 리뷰. 지정된 파일들을 하나씩 읽어 구조를 분석합니다.

    클로드 코드가 이 리뷰 결과를 바탕으로 Aider 프롬프트를 구성합니다.
    각 파일의 Status 헤더 → 구조(imports, functions, classes) → 작업 연관성을
    순차적으로 평가하여 반환합니다.

    Args:
        task: 수행할 코딩 작업 설명 (자연어)
        paths: 리뷰할 파일 경로들 (쉼표 구분, /scripts/ 기준 상대 경로)
    """
    import re
    from pathlib import Path

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

        # header extraction
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

        # structure analysis
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


# ── Entry point ────────────────────────────────────────────────


def _create_app():
    """Create and configure the ASGI app with health endpoint."""
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    app = mcp.streamable_http_app()

    async def health_endpoint(request):
        return JSONResponse(
            {
                "status": "ok",
                "server": "devforge-mcp",
            }
        )

    app.router.routes.insert(0, Route("/health", health_endpoint, methods=["GET"]))
    return app


def main():
    """Start the MCP server with uvicorn (Starlette app from FastMCP)."""
    import argparse

    parser = argparse.ArgumentParser(description="DevForge MCP Server")
    parser.add_argument(
        "--port", "-p", type=int, default=8000, help="Port to listen on (default: 8000)"
    )
    parser.add_argument(
        "--host", type=str, default="127.0.0.1", help="Host to bind (default: 127.0.0.1)"
    )
    args = parser.parse_args()
    import uvicorn

    app = _create_app()
    logger.info("Starting DevForge MCP server on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
