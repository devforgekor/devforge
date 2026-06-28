#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh — FTS5 refresh phase (post text_clean)
"""FTS5 Refresh — text_clean 기준 FTS5 인덱스 동기화.

text_clean.py 실행 후 호출. pipeline_state = 'cleaned'인 turn의
text_clean/thinking_clean을 FTS5 인덱스에 반영.

Backward compat: 기존 'polished' 상태도 포함 (unified preprocessing merge 이전).
"""
from __future__ import annotations

import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_json
from lib.search.local_index import FTS5Index


def get_stale_turn_ids(limit: int = 500) -> list[str]:
    """Return turn IDs with text_clean set but FTS5 may be stale.

    Compares text_clean vs COALESCE(text_clean_polished, text_clean) to detect
    newly unified clean text that needs FTS5 sync.
    """
    rows = psql_json(
        f"SELECT id FROM turns "
        f"WHERE (pipeline_state IN ('cleaned', 'polished')) "
        f"  AND text_clean IS NOT NULL AND text_clean != '' "
        f"ORDER BY id ASC "
        f"LIMIT {limit}"
    )
    return [r["id"] for r in rows] if rows else []


def main():
    t0 = time.monotonic()
    print("=" * 60, flush=True)
    print("FTS5 Refresh — text_clean sync (unified preprocessing)", flush=True)
    print("=" * 60, flush=True)

    idx = FTS5Index()

    total_updated = 0
    total_skipped = 0
    total_errors = 0
    limit = 500

    while True:
        turn_ids = get_stale_turn_ids(limit)
        if not turn_ids:
            break

        result = idx.refresh_turns(turn_ids)
        total_updated += result["updated"]
        total_skipped += result["skipped"]
        total_errors += result["errors"]

        if len(turn_ids) < limit:
            break

    elapsed = time.monotonic() - t0
    print(f"  done: {total_updated} updated, {total_skipped} skipped"
          f" (not in FTS5), {total_errors} errors, {elapsed:.1f}s", flush=True)

    if total_errors > 0:
        print(f"  [warn] {total_errors} batches had errors — check logs", flush=True)


if __name__ == "__main__":
    main()
