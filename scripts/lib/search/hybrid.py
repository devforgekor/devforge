#!/usr/bin/env python3
# Status: experimental
# Path: imported by CLI search, MCP search tools
"""Hybrid search — Reciprocal Rank Fusion (RRF) of FTS5 BM25 + pgvector ANN.

RRF formula: score(d) = Σ 1 / (k + rank_i(d))  where k = 60 (default)

Two independent searches:
  1. BM25 (FTS5 via local_index.py) — term-based, lower rank = better
  2. Dense (pgvector cosine) — semantic, lower rank = better

RRF combines ranks: combined rank = 1/(k + bm25_rank) + 1/(k + dense_rank)

Requires:
  - lib/search/local_index.FTS5Index (rebuild first)
  - turns.embedding populated via embeddings table with embed model vectors
  - pgvector HNSW index on embedding

Usage:
  python3 -c "from lib.search.hybrid import hybrid_search; print(hybrid_search('질문'))"
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Dict, List, Optional, Tuple

from lib.db import psql, psql_json, esc_sql
from lib.search.local_index import FTS5Index

# Embed API (same endpoint as embed_batch.py)
from lib.llm_client import MODEL_REGISTRY
EMBED_URL = f"http://127.0.0.1:{MODEL_REGISTRY['embedder']['port']}/v1/embeddings"
EMBED_TIMEOUT = 30

# RRF constant
RRF_K = 60

# Dense ANN search limit per call
DENSE_SEARCH_LIMIT = 100


def _get_query_vector(query: str) -> Optional[List[float]]:
    """Get embedding vector for a query string via embed API."""
    body = json.dumps({"input": query, "model": "default"}).encode()
    try:
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data["data"][0]["embedding"]
        return vec
    except Exception:
        return None


def _bm25_rank(query: str, limit: int = 50) -> Dict[str, int]:
    """BM25 search via FTS5. Returns {turn_id: rank} (0-based, lower=better)."""
    idx = FTS5Index()
    results = idx.bm25_search(query, limit=limit)
    return {r["turn_id"]: i for i, r in enumerate(results)}


def _dense_rank(query: str, limit: int = DENSE_SEARCH_LIMIT) -> Tuple[Dict[str, int], Optional[str]]:
    """Dense ANN search via pgvector cosine distance. Returns {turn_id: rank}, error_msg."""
    vec = _get_query_vector(query)
    if vec is None:
        return {}, "embed API unavailable"

    # Format as pgvector literal
    vec_str = "[" + ",".join(f"{v:.8f}" for v in vec) + "]"

    sql = (
        f"SELECT t.id, (e.embedding <=> '{esc_sql(vec_str)}'::vector) as dist "
        f"FROM turns t "
        f"JOIN embeddings e ON e.source_type = 'turn' AND e.source_id = t.id "
        f"  AND e.model_name = 'qwen3-embedding-8b-v1' "
        f"ORDER BY e.embedding <=> '{esc_sql(vec_str)}'::vector "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql) or []
    return {row["id"]: i for i, row in enumerate(rows)}, None


def _rrf_score(rank: int) -> float:
    """Compute RRF score from rank (0-based)."""
    return 1.0 / (RRF_K + rank + 1)


def hybrid_search(query: str, limit: int = 20) -> Dict:
    """Hybrid BM25 + Dense search via RRF fusion.

    Args:
        query: Natural language query.
        limit: Max results to return.

    Returns:
        {
            results: [{turn_id, conversation_id, created_at, agent, seq,
                       text_clean, bm25_rank, dense_rank, rrf_score}],
            meta: {bm25_count, dense_count, bm25_time, dense_time, embed_error}
        }
    """
    meta: Dict = {"bm25_count": 0, "dense_count": 0,
                  "bm25_time": 0.0, "dense_time": 0.0}

    # --- Phase 1: BM25 ---
    t0 = time.monotonic()
    bm25_ranks = _bm25_rank(query, limit=50)
    meta["bm25_time"] = round(time.monotonic() - t0, 3)
    meta["bm25_count"] = len(bm25_ranks)

    # --- Phase 2: Dense ---
    t0 = time.monotonic()
    dense_ranks, embed_error = _dense_rank(query)
    meta["dense_time"] = round(time.monotonic() - t0, 3)
    meta["dense_count"] = len(dense_ranks)
    if embed_error:
        meta["embed_error"] = embed_error

    # --- RRF Fusion ---
    all_turn_ids = set(bm25_ranks.keys()) | set(dense_ranks.keys())
    scored: List[tuple] = []
    for tid in all_turn_ids:
        score = 0.0
        b_rank = bm25_ranks.get(tid)
        d_rank = dense_ranks.get(tid)
        if b_rank is not None:
            score += _rrf_score(b_rank)
        if d_rank is not None:
            score += _rrf_score(d_rank)
        scored.append((score, tid, b_rank, d_rank))

    # Sort by RRF score descending
    scored.sort(key=lambda x: (-x[0], x[1]))

    # --- Enrich results ---
    top = scored[:limit]
    if not top:
        return {"results": [], "meta": meta}

    # Fetch metadata for top results
    tid_list = [r[1] for r in top]
    tid_quoted = ",".join(f"'{esc_sql(t)}'::uuid" for t in tid_list)
    meta_rows = psql_json(
        f"SELECT id, conversation_id, created_at::text, agent, seq, "
        f"  text_clean "
        f"FROM turns "
        f"WHERE id IN ({tid_quoted})"
    ) or []
    meta_map = {r["id"]: r for r in meta_rows}

    results = []
    for score, tid, b_rank, d_rank in top:
        info = meta_map.get(tid, {})
        results.append({
            "turn_id": tid,
            "conversation_id": info.get("conversation_id", ""),
            "created_at": info.get("created_at", ""),
            "agent": info.get("agent", ""),
            "seq": info.get("seq"),
            "text_clean": (info.get("text_clean") or "")[:200],
            "bm25_rank": b_rank,
            "dense_rank": d_rank,
            "rrf_score": round(score, 4),
        })

    return {"results": results, "meta": meta}


def bm25_only(query: str, limit: int = 20) -> Dict:
    """BM25-only search (no dense component)."""
    t0 = time.monotonic()
    idx = FTS5Index()
    results = idx.bm25_search(query, limit=limit)
    elapsed = round(time.monotonic() - t0, 3)
    return {
        "results": [{
            "turn_id": r["turn_id"],
            "conversation_id": r["conversation_id"],
            "created_at": r["created_at"],
            "agent": r["agent"],
            "seq": r["seq"],
            "text_clean": (r.get("text_clean") or "")[:200],
            "rank": round(r["rank"], 4),
        } for r in results],
        "meta": {"count": len(results), "elapsed_s": elapsed},
    }
