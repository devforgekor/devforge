#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 2 (extract chain)
"""Day-time chain: extract -> MCP enrich (Pod B :8082, checkpoint-based).

Runs as Phase 2 of day_cycle.sh. extract.py then mcp_enrich.py.
Pod B (:8082) must be in day mode before calling this.
"""

import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.infra.preflight import preflight_checks


def log(msg: str) -> None:
    log_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{log_ts}] {msg}", flush=True)


def main() -> None:
    t_start = time.monotonic()
    log("=" * 60)
    log("DevForge Day Cycle — extract -> MCP enrich")
    log("=" * 60)

    preflight_checks("day_cycle.py", required_ports={8082})

    # Phase 1: Extract
    log("\n=== Phase 1: Extract ===")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [sys.executable, "-u", "extract.py", "--limit", "50"],
            capture_output=True, text=True, timeout=300,
        )
        log(f"  [elapsed] {time.monotonic() - t0:.0f}s")
        if r.returncode == 0:
            log("  [ok] exit=0")
        else:
            log(f"  [warn] exit={r.returncode}")
            if r.stderr:
                log(f"  [stderr] {r.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log(f"  [elapsed] {time.monotonic() - t0:.0f}s")
        log("  [warn] extract timed out (300s) — partial results preserved via checkpoint")

    # Phase 2: MCP Enrich
    log("\n=== Phase 2: MCP Enrich ===")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [sys.executable, "-u", "mcp_enrich.py", "--limit", "50"],
            capture_output=True, text=True, timeout=300,
        )
        log(f"  [elapsed] {time.monotonic() - t0:.0f}s")
        for line in r.stdout.split("\n"):
            log(f"  {line}")
        if r.returncode == 0:
            log("  [ok] exit=0")
        else:
            log(f"  [warn] exit={r.returncode}")
            if r.stderr:
                log(f"  [stderr] {r.stderr[:200]}")
    except subprocess.TimeoutExpired:
        log(f"  [elapsed] {time.monotonic() - t0:.0f}s")
        log("  [warn] MCP enrich timed out (300s) — partial results preserved via checkpoint")

    log(f"\n{'=' * 60}")
    log(f"Day Cycle complete in {time.monotonic() - t_start:.0f}s")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
