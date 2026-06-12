#!/usr/bin/env python3
# Status: experimental
# Path: day_extract.sh → systemd:devforge-day-extract.timer (:00)
"""Day Extract Pipeline — extract → MCP enrich on Pod B (7B:8082).

Called at :00 by systemd timer. Runs extraction then MCP enrichment
on the Pod B Qwen2.5-Coder-7B extractor (8082). DB only — no eval/ output.
Checkpoint-based resume: next cycle picks up from last checkpoint.

Usage:
  python3 scripts/pipelines/day_extract.py [--limit 50] [--dry-run]
"""

import os
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.infra.preflight import preflight_checks
from lib.db import psql_json
from lib.common import log

BATCH_LIMIT = 50
MAX_BUDGET = 1500      # 25 minutes
BUFFER_MIN = 180       # 3 minutes — defer if remaining exceeds this
STARVATION_LIMIT = 3   # consecutive defers before alert


def _check_backlog() -> int:
    """Count turns needing extraction since last checkpoint."""
    sql = (
        "SELECT count(*) FROM turns t "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type IN ('text','thinking','user')"
        ")"
    )
    rows = psql_json(sql)
    return int(rows[0]["count"]) if rows else 0


def _count_mcp_backlog() -> int:
    """Count extracted turns still needing MCP enrichment."""
    sql = (
        "SELECT count(*) FROM turns t "
        "WHERE EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type IN ('text','thinking','user')"
        ")"
        "AND NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id AND rf.fact_type = 'mcp_meta'"
        ")"
    )
    rows = psql_json(sql)
    return int(rows[0]["count"]) if rows else 0


def main() -> None:
    t_start = time.monotonic()
    consecutive_defer = 0

    log("=" * 60)
    log("DevForge Day Extract — Pod B (7B:8082) extract + MCP enrich")
    log("=" * 60)

    preflight_checks("day_extract.py")

    # Main loop: process batches until budget runs out or backlog clear
    while True:
        elapsed = time.monotonic() - t_start
        remaining = MAX_BUDGET - elapsed

        # Check backlog before each batch
        backlog = _check_backlog()
        if backlog == 0:
            log(f"[done] No backlog remaining ({elapsed:.0f}s)")
            break

        if remaining < BUFFER_MIN:
            log(f"[buffer] Remaining budget {remaining:.0f}s < {BUFFER_MIN}s — deferring {backlog} turns")
            consecutive_defer += 1
            if consecutive_defer >= STARVATION_LIMIT:
                log(f"[alert] Extract backlog: {backlog} turns, {consecutive_defer}x consecutive defer")
                # Log to worklog or stderr — operator will see in logs
            break

        consecutive_defer = 0
        log(f"\n=== Batch: extract (backlog={backlog}, budget={remaining:.0f}s) ===")

        # Phase 1: Extract
        from extract import extract_pipeline
        ex_result = extract_pipeline(limit=BATCH_LIMIT)
        if not ex_result.get("ok", True):
            log(f"[warn] Extract partial failure: {ex_result}")

        # Phase 2: MCP enrich
        mcp_backlog = _count_mcp_backlog()
        if mcp_backlog > 0 and (time.monotonic() - t_start) < (MAX_BUDGET - BUFFER_MIN):
            log(f"\n=== Batch: MCP enrich (backlog={mcp_backlog}) ===")
            from mcp_enrich import mcp_enrich_pipeline
            mcp_result = mcp_enrich_pipeline(limit=BATCH_LIMIT)
            if not mcp_result.get("ok", True):
                log(f"[warn] MCP enrich partial failure: {mcp_result}")

    total = round(time.monotonic() - t_start, 1)
    log(f"\n{'=' * 60}")
    log(f"Day Extract complete in {total}s")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
