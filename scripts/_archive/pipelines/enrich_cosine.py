#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — embed phase (after embed_batch.py)
"""Enrich Cosine Verification — embed model based entity/tldr cosine check.

Runs during embed phase (:8081) after embed_batch.py.
Finds enrich_meta rows with cosine_status=pending|deferred, embeds entity/tldr
text via embed model, compares with embeddings table.

State machine: pending → done|deferred → done|failed (max 3 retries).
Deferred rows with embedding NULL retry next cycle via day_cycle.sh guard.

Usage:
  python3 scripts/pipelines/enrich_cosine.py              # batch from pending|deferred
  python3 scripts/pipelines/enrich_cosine.py --turn-id <uuid>  # single turn (debug)
  python3 scripts/pipelines/enrich_cosine.py --limit 20   # batch cap
  python3 scripts/pipelines/enrich_cosine.py --dry-run    # simulate, no writes
"""

import json
import math
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, psql_json, esc_sql
from lib.infra.preflight import preflight_checks
from lib.common import log

from lib.llm_client import MODEL_REGISTRY
EMBED_URL = f"http://127.0.0.1:{MODEL_REGISTRY['embedder']['port']}/v1/embeddings"
EMBED_TIMEOUT = 120   # per entity/tldr request
BATCH_LIMIT = 20
MAX_CYCLE = 300        # 5min max

# Cosine thresholds — embed model 4096d embedding space
COSINE_ENTITY_RELEVANCE = 0.25
TLDR_COSINE_MIN = 0.30


def _parse_pg_vector(raw: str) -> Optional[List[float]]:
    """Parse PostgreSQL vector literal like '[0.123,0.456,...]'."""
    if not raw:
        return None
    try:
        return [float(x) for x in raw.strip("[]").split(",")]
    except (ValueError, AttributeError):
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_text(text: str) -> Optional[List[float]]:
    """Request 4096-dim vector from embed model; returns None on failure."""
    clean = text.strip()
    if not clean:
        return None
    body = json.dumps({"input": clean, "model": "default"}).encode()
    try:
        req = urllib.request.Request(
            EMBED_URL, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data["data"][0]["embedding"]
        if len(vec) != 4096:
            log(f"  [warn] unexpected embed dim {len(vec)} (expected 4096)")
        return vec
    except Exception as e:
        log(f"  [error] embed failed: {e}")
        return None


def _get_pending_enrich(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Review_facts rows with cosine_status=pending or deferred, with turn embedding."""
    sql = (
        "SELECT rf.turn_id, rf.evidence, rf.fact_index, rf.extract_model, "
        "  rf.created_at::text, e.embedding::text AS vec "
        "FROM review_facts rf "
        "JOIN embeddings e ON e.source_type = 'turn' AND e.source_id = rf.turn_id "
        "  AND e.model_name = 'qwen3-embedding-8b-v1' "
        "WHERE rf.fact_type = 'enrich_meta' "
        "  AND (rf.evidence LIKE '%\"cosine_status\":\"pending\"%' "
        "    OR rf.evidence LIKE '%\"cosine_status\":\"deferred\"%') "
        "ORDER BY rf.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    return [{
        "turn_id": r.get("turn_id", ""),
        "evidence": r.get("evidence", ""),
        "fact_index": r.get("fact_index", 0),
        "extract_model": r.get("extract_model", ""),
        "created_at": r.get("created_at", ""),
        "vec": r.get("vec", ""),
    } for r in rows]


def _get_pending_by_turn(turn_id: str) -> Optional[Dict[str, Any]]:
    """Single turn lookup for --turn-id mode (pending or deferred)."""
    sql = (
        f"SELECT rf.turn_id, rf.evidence, rf.fact_index, rf.extract_model, "
        f"  rf.created_at::text, e.embedding::text AS vec "
        f"FROM review_facts rf "
        f"JOIN embeddings e ON e.source_type = 'turn' AND e.source_id = rf.turn_id "
        f"  AND e.model_name = 'qwen3-embedding-8b-v1' "
        f"WHERE rf.turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND rf.fact_type = 'enrich_meta' "
        f"  AND (rf.evidence LIKE '%\"cosine_status\":\"pending\"%' "
        f"    OR rf.evidence LIKE '%\"cosine_status\":\"deferred\"%')"
    )
    rows = psql_json(sql)
    if not rows:
        return None
    r = rows[0]
    return {
        "turn_id": r.get("turn_id", ""),
        "evidence": r.get("evidence", ""),
        "fact_index": r.get("fact_index", 0),
        "extract_model": r.get("extract_model", ""),
        "created_at": r.get("created_at", ""),
        "vec": r.get("vec", ""),
    }


def _verify_entity_cosine(entities: Dict, turn_vec: List[float]) -> Dict:
    """Embed each entity text via embed model, compare with turn embedding.

    Returns dict keyed by entity category with per-entity cosine scores.
    """
    result = {}
    for cat in ("technologies", "functions", "files", "mentioned_users"):
        items = entities.get(cat, []) if isinstance(entities, dict) else []
        if not items or not isinstance(items, list):
            result[cat] = []
            continue
        cat_results = []
        for entity_name in items[:5]:
            entity_str = str(entity_name).strip()
            if not entity_str:
                continue
            entity_vec = _embed_text(entity_str)
            if entity_vec is None:
                cat_results.append({
                    "entity": entity_str, "cosine": 0.0, "relevant": False,
                    "error": "embed_failed",
                })
                continue
            score = _cosine(entity_vec, turn_vec)
            cat_results.append({
                "entity": entity_str,
                "cosine": round(float(score), 3),
                "relevant": score >= COSINE_ENTITY_RELEVANCE,
            })
        result[cat] = cat_results
    return result


def _verify_tldr_cosine(tldr: str, turn_vec: List[float]) -> float:
    """Embed tldr via embed model, compare with turn embedding."""
    if not tldr or not tldr.strip():
        return 0.0
    tldr_vec = _embed_text(tldr.strip())
    if tldr_vec is None:
        return 0.0
    return round(_cosine(tldr_vec, turn_vec), 3)


def _update_enrich_meta(turn_id: str, enrich_json_str: str,
                     extract_model: str = "") -> bool:
    """Update evidence JSON for an existing enrich_meta row."""
    sql = (
        f"UPDATE review_facts SET evidence = '{esc_sql(enrich_json_str[:5000])}' "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND fact_type = 'enrich_meta'"
        + (f"  AND extract_model = '{esc_sql(extract_model)}'" if extract_model else "")
    )
    return psql_ok(sql)


def enrich_cosine_pipeline(turn_id: Optional[str] = None,
                        limit: int = BATCH_LIMIT,
                        dry_run: bool = False) -> Dict[str, Any]:
    """Verify pending enrich cosine via embed model against embeddings table."""
    t_start = time.monotonic()
    log("=" * 60)
    log("Enrich Cosine Verify — embed model vs embeddings")
    if dry_run:
        log("  [DRY RUN] No writes to DB")
    log("=" * 60)

    if turn_id:
        row = _get_pending_by_turn(turn_id)
        rows = [row] if row else []
        if not rows:
            log(f"  [ok] Turn {turn_id[:8]} — no pending enrich_meta")
            return {"processed": 0, "failed": 0, "ok": True}
    else:
        rows = _get_pending_enrich(limit)

    if not rows:
        log("  [ok] No pending enrich_meta to verify")
        return {"processed": 0, "failed": 0, "ok": True}

    log(f"  Found {len(rows)} pending enrich_meta row(s)")

    processed = 0
    failed = 0
    deferred = 0

    for ri, row in enumerate(rows, 1):
        if time.monotonic() - t_start > MAX_CYCLE:
            log(f"  [timeout] cycle limit ({MAX_CYCLE}s) — {ri-1} done, remaining deferred")
            break
        turn_id = row["turn_id"]
        evidence_raw = row.get("evidence", "") or ""
        f16_raw = row.get("vec", "") or ""
        created = row.get("created_at", "")[:19]

        log(f"  [{ri}/{len(rows)}] {turn_id[:8]} {created}")

        # Parse enrich evidence JSON (needed for skip_count even if f16 NULL)
        try:
            enrich_data = json.loads(evidence_raw)
        except json.JSONDecodeError:
            log(f"    skip — evidence JSON parse error")
            failed += 1
            continue

        # Embedding_f16 NULL → deferred or failed
        turn_vec = _parse_pg_vector(f16_raw)
        if turn_vec is None or len(turn_vec) != 4096:
            skip_count = enrich_data.get("skip_count", 0) + 1
            enrich_data["skip_count"] = skip_count
            if skip_count >= MAX_RETRY:
                enrich_data["cosine_status"] = "failed"
                enrich_data["error_code"] = "MAX_RETRY_EXCEEDED"
                log(f"    → failed (embedding NULL ×{skip_count})")
                failed += 1
            else:
                enrich_data["cosine_status"] = "deferred"
                log(f"    → deferred (embedding NULL ×{skip_count})")
                deferred += 1
            updated_json = json.dumps(enrich_data, ensure_ascii=False)
            if not dry_run:
                _update_enrich_meta(turn_id, updated_json, row.get("extract_model", ""))
            continue

        # Embedding_f16 present — run cosine checks
        entities = enrich_data.get("entities", {})
        tldr = enrich_data.get("tldr", "") or ""

        # Entity cosine grounding
        try:
            cosine_grounding = _verify_entity_cosine(entities, turn_vec)
            enrich_data["cosine_grounding"] = cosine_grounding
            low_rel = sum(
                1 for cat in cosine_grounding.values()
                for e in cat if not e.get("relevant", True))
            embed_errors = sum(
                1 for cat in cosine_grounding.values()
                for e in cat if e.get("error"))
            parts = [f"{low_rel} low-relevance"]
            if embed_errors:
                parts.append(f"{embed_errors} embed-failed")
            log(f"    entity cosine: {', '.join(parts)}")
        except Exception as e:
            log(f"    entity cosine skipped: {e}")

        # TLDR cosine quality
        if tldr:
            try:
                tq = _verify_tldr_cosine(tldr, turn_vec)
                enrich_data["tldr_cosine_8b"] = tq
                flag = " (LOW)" if tq < TLDR_COSINE_MIN else ""
                log(f"    tldr cosine: {tq}{flag}")
            except Exception as e:
                log(f"    tldr cosine skipped: {e}")

        enrich_data["cosine_status"] = "done"
        updated_json = json.dumps(enrich_data, ensure_ascii=False)

        if dry_run:
            log(f"    [DRY] Would update enrich_meta ({len(updated_json)} chars)")
            processed += 1
            continue

        if _update_enrich_meta(turn_id, updated_json, row.get("extract_model", "")):
            log(f"    updated ✓")
            processed += 1
        else:
            log(f"    update FAILED")
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    parts = [f"{processed} ok"]
    if deferred:
        parts.append(f"{deferred} deferred")
    parts.append(f"{failed} failed")
    log(f"Enrich cosine done: {', '.join(parts)} ({elapsed}s)")
    if dry_run:
        log("  [DRY RUN] No data was written")

    return {"processed": processed, "failed": failed,
            "deferred": deferred, "elapsed_s": elapsed,
            "ok": failed == 0}


def main() -> None:
    preflight_checks("enrich_cosine.py", required_ports={8081})
    import argparse
    parser = argparse.ArgumentParser(
        description="Enrich Cosine Verify — embed model vs embeddings")
    parser.add_argument("--turn-id", help="Process a specific turn UUID")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = enrich_cosine_pipeline(
        turn_id=args.turn_id,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
