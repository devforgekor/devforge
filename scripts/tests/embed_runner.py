#!/usr/bin/env python3
# Status: experimental
# Path: background — full embed run with watchdog progress
"""Full Embed Runner — continuous embed_batch.py loop with watchdog progress.

Writes progress to catchdog_events table every N batches so Watchdog
can monitor and report. Runs until all turns are embedded or killed.

Usage:
  python3 scripts/pipelines/embed_runner.py                    # full run
  python3 scripts/pipelines/embed_runner.py --limit 200        # 200 per cycle
"""

import os
import sys
import subprocess as sp
import time

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql_ok
from lib.protection import register_protect, unregister_protect
from lib.test_common import test_setup, test_heartbeat, test_complete, log, psql_json
from lib.pipeline_common import slack_send
SLACK_INTERVAL = 1800       # Slack report every 30min
EVENT_INTERVAL = 300        # catchdog_events every 5min
EMBED_SCRIPT = os.path.join(SCRIPTS_DIR, 'pipelines', 'embed_batch.py')
BATCH_LIMIT = 200           # turns per embed_batch.py cycle


def _write_event(detail: str):
    """Write a watchable event to catchdog_events."""
    psql_ok(
        "INSERT INTO catchdog_events (component, event_type, detail) VALUES "
        f"('embed_batch', 'progress', '{detail[:200]}')"
    )


def main():
    TEST = test_setup("embed_runner", "Full embed runner with watchdog progress")
    dry_run = "--dry-run" in sys.argv
    limit = BATCH_LIMIT
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    # Create protection
    if not dry_run:
        register_protect("embed_runner", reason="Full embed runner with watchdog progress")
        print("[embed-runner] Protection registered via lib.protection")

    # Get starting counts
    total = int(psql_json("SELECT COUNT(*) as cnt FROM turns")[0]["cnt"])

    _write_event(f"Embed runner started: {total} turns total")

    t_start = time.monotonic()
    last_slack = 0
    last_event = 0
    total_processed = 0
    cycle = 0

    while True:
        cycle += 1

        # Check remaining
        remaining_rows = psql_json(
            "SELECT COUNT(*) as cnt FROM turns WHERE embedding IS NULL"
        )
        remaining = int(remaining_rows[0]["cnt"]) if remaining_rows else 0

        if remaining == 0:
            done_msg = f"Embedding complete: {total}/{total} turns"
            print(f"[embed-runner] {done_msg}")
            _write_event(done_msg)
            slack_send(f"✅ Embedding *complete* — {total}/{total} turns")
            if not dry_run:
                unregister_protect("embed_runner")
            break

        # Run one cycle
        print(f"\n[embed-runner] Cycle {cycle}: {remaining} remaining (limit={limit})")
        cmd = [sys.executable, EMBED_SCRIPT, "--limit", str(limit)]
        if dry_run:
            cmd.append("--dry-run")

        t0 = time.monotonic()
        result = sp.run(cmd, capture_output=True, text=True)
        elapsed = time.monotonic() - t0

        # Print output
        for line in result.stdout.split("\n"):
            if line.strip():
                print(f"  {line.strip()}")
        if result.stderr and result.stderr.strip():
            for line in result.stderr.split("\n"):
                if line.strip():
                    print(f"  [stderr] {line.strip()}")

        if result.returncode != 0:
            print(f"  [embed-runner] embed_batch.py exit code {result.returncode}")
            # Log error but continue — checkpoint doesn't advance on failures
            _write_event(f"Cycle {cycle}: exit {result.returncode}, {remaining} remain, continuing")

        # Count progress
        done_rows = psql_json(
            "SELECT COUNT(*) as cnt FROM turns WHERE embedding IS NOT NULL"
        )
        done = int(done_rows[0]["cnt"]) if done_rows else 0
        cycle_progress = done - total_processed
        total_processed = done
        pct = round(done / total * 100, 1) if total > 0 else 0

        # Periodic event to catchdog_events
        now = time.monotonic()
        if now - last_event >= EVENT_INTERVAL or cycle == 1:
            _write_event(f"Cycle {cycle}: {cycle_progress} processed, {done}/{total} ({pct}%), {remaining} remain")
            last_event = now

        # Periodic Slack report
        if now - last_slack >= SLACK_INTERVAL or cycle == 1:
            bar_len = 16
            filled = int(bar_len * done / total) if total > 0 else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            msg = f"*Embed Progress* — Qwen3-Embedding-8B\n{bar} {pct}%  ({done}/{total} 완료, {remaining} 남음)"
            slack_send(msg)
            last_slack = now

        print(f"  [embed-runner] Cycle {cycle}: {cycle_progress} in {elapsed:.0f}s, total={done}/{total} ({pct}%)")

    elapsed = time.monotonic() - t_start
    print(f"\n[embed-runner] Done: {total_processed} processed, {elapsed:.0f}s total")
    test_complete("success")


if __name__ == "__main__":
    main()
