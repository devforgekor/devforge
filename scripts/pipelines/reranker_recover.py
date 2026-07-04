#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh:380 — re-score RERANKER_ERROR facts after inference recovery
"""Reranker Recovery — quarantine + retry for RERANKER_ERROR facts.

Selects facts where faithful_method='reranker_err' (previous reranker failure),
re-runs reranker scoring, and updates faithful_score/faitful_method/nli_verdict.

Designed for day_cycle.sh between extract and enrich:
  extract → reranker_recover (if inference healthy) → enrich → verify
"""

import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql
from lib.llm_client import reranker_score, reranker_nli_verdict


# ── Health check ─────────────────────────────────────────────


def _reranker_healthy() -> bool:
    """Quick health check via /health. Returns True if inference responds."""
    import urllib.request
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=5)
        return resp.status == 200
    except Exception:
        return False


# ── Recovery ─────────────────────────────────────────────────


def recover_reranker_errors(limit: int = 50) -> int:
    """Re-score facts with RERANKER_ERROR. Returns count of updated facts."""
    if not _reranker_healthy():
        print("[reranker_recover] inference reranker NOT healthy — skipping", flush=True)
        return -1

    rows = psql_json(
        f"SELECT rf.id::text, rf.evidence, "
        f"  left(rf.evidence, 60) AS evidence_preview, "
        f"  t.user_turn, t.thinking, t.text, rf.fact_type "
        f"FROM review_facts rf "
        f"JOIN turns t ON t.id = rf.turn_id "
        f"WHERE rf.faithful_method = 'reranker_err' "
        f"ORDER BY rf.created_at DESC "
        f"LIMIT {limit}",
        timeout=30,
    )
    if not rows:
        print("[reranker_recover] No RERANKER_ERROR facts found", flush=True)
        return 0

    source_map = {"user": "user_turn", "thinking": "thinking", "text": "text"}

    updated = 0
    for r in rows:
        evidence = r.get("evidence", "")
        src_col = source_map.get(r.get("fact_type", ""), "text")
        source = r.get(src_col, "")

        if not evidence or not source:
            # No source to compare — skip with a non-error fallback
            psql_ok(
                f"UPDATE review_facts SET faithful_method = 'reranker_nosrc', "
                f"nli_verdict = 'AMBIGUOUS', faithful_score = 0.0 "
                f"WHERE id = '{esc_sql(r['id'])}'::uuid "
                f"AND faithful_method = 'reranker_err'"
            )
            updated += 1
            continue

        score = reranker_score(evidence, source)
        grounding = reranker_nli_verdict(score)

        if grounding == "RERANKER_ERROR":
            # Still failing — skip this fact, retry next time
            continue

        psql_ok(
            f"UPDATE review_facts SET "
            f"  faithful_score = {score:.4f}, "
            f"  faithful_method = 'reranker_recovered', "
            f"  nli_verdict = '{esc_sql(grounding)}' "
            f"WHERE id = '{esc_sql(r['id'])}'::uuid "
            f"AND faithful_method = 'reranker_err'"
        )
        updated += 1

    print(f"[reranker_recover] Updated {updated}/{len(rows)} facts", flush=True)
    return updated


# ── CLI ──────────────────────────────────────────────────────


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Recover RERANKER_ERROR facts")
    parser.add_argument("--limit", type=int, default=50, help="Max facts to process")
    args = parser.parse_args()

    count = recover_reranker_errors(limit=args.limit)
    if count < 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
