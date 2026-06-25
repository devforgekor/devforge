#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — re-embed phase (after verify, before 2차 embedding)
"""Re-embed Check — detect content_hash changes → reset embedding=NULL.

After polish/MCP/verify stages, embedding text may have changed (e.g.
text_clean_polished was created or updated). This script compares stored
content_hash with current hash of COALESCE(text_clean_polished, text_clean).
If different, the embedding is stale → reset embedding=NULL so the
next embed_batch cycle picks it up for re-embedding.

Usage:
  python3 scripts/pipelines/re_embed_check.py [--dry-run]
  python3 scripts/pipelines/re_embed_check.py [--limit 50]
"""

import hashlib
import sys
import time

import os
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json, psql_ok, esc_sql
from lib.watchdog.messenger import heartbeat


def _embed_text(row: dict) -> str:
    """Compute what embed_batch would use as text for this turn."""
    text = row.get("text_clean_polished") or row.get("text_clean") or ""
    user = row.get("user_turn_clean_polished") or row.get("user_turn_clean") or ""
    return f"{user} {text}".strip()


def main():
    dry_run = "--dry-run" in sys.argv
    limit = 200
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    heartbeat("re_embed_check")
    print("=" * 60, flush=True)
    print("Re-embed Check — stale embedding detection", flush=True)
    print("=" * 60, flush=True)

    t_start = time.monotonic()

    rows = psql_json(
        f"SELECT t.id, t.user_turn_clean, t.text_clean, "
        f"  t.user_turn_clean_polished, t.text_clean_polished, "
        f"  t.content_hash "
        f"FROM turns t "
        f"JOIN embeddings e ON e.source_type = 'turn' AND e.source_id = t.id "
        f"  AND e.model_name = 'qwen3-embedding-8b-v1' "
        f"WHERE t.content_hash IS NOT NULL "
        f"LIMIT {limit}"
    )
    if not rows:
        print("  [ok] No turns with content_hash — nothing to check", flush=True)
        return

    n_reset = 0
    for row in rows:
        turn_id = row["id"]
        stored_hash = row.get("content_hash", "")
        if not stored_hash:
            continue

        current_text = _embed_text(row)
        if not current_text:
            continue

        current_hash = hashlib.sha256(current_text.encode()).hexdigest()
        if current_hash == stored_hash:
            continue

        # Hash changed — embedding is stale
        if dry_run:
            print(f"  DRY-RUN: {turn_id[:8]} hash changed → would delete embedding", flush=True)
        else:
            psql_ok(
                f"DELETE FROM embeddings "
                f"WHERE source_type = 'turn' AND source_id = '{esc_sql(turn_id)}'::uuid "
                f"  AND model_name = 'qwen3-embedding-8b-v1'",
                timeout=15,
            )
        n_reset += 1

    elapsed = time.monotonic() - t_start
    print(f"Re-embed check done: {n_reset} turns need re-embedding, {elapsed:.1f}s", flush=True)
    print(f"  (embed_batch will pick them up next)", flush=True)


if __name__ == "__main__":
    main()
