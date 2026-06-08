#!/usr/bin/env python3
# Status: experimental
# Path: 15m_cycle.sh extract — daytime chain at :00/:30
"""Day-time chain: extract → py verify → 7B verify → global context.

Runs on :00/:30 timers. extract.py then night.py --phases 1 2.
Global context (category_summary) saved to eval/ for :15/:45 classify.
4 cores dedicated — phases run sequentially, no contention.
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
    log("DevForge Day Cycle — extract → py verify → 7B verify → global context")
    log("=" * 60)

    preflight_checks("day_cycle.py", required_ports={8080})

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

    # Phase 2: Py Verify + 7B Verify + Global Context
    log("\n=== Phase 2: Py Verify + 7B Verify + Global Context ===")
    log("  (night.py --phases 1 2 on Qwen7B :8080, 4 cores)")
    t0 = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-u", "night.py", "--phases", "1", "2"],
        capture_output=True, text=True, timeout=1800,
    )
    elapsed = time.monotonic() - t0
    for line in r.stdout.split("\n"):
        log(f"  {line}")
    if r.returncode == 0:
        log(f"  [ok] complete in {elapsed:.0f}s")
    else:
        log(f"  [warn] night.py exit={r.returncode} in {elapsed:.0f}s")
        if r.stderr:
            log(f"  [stderr] {r.stderr[:300]}")

    log(f"\n{'=' * 60}")
    log(f"Day Cycle complete in {time.monotonic() - t_start:.0f}s")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
