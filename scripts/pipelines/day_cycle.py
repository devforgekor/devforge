#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 2-4 (extract → enrich → verify)
"""Day-time chain: extract -> enrich -> verify (Pod B :8082, checkpoint-based).

Runs as Phase 2-4 of day_cycle.sh. extract.py then enrich.py then day_verify.py.
Pod B (:8082) must be in day mode during extract/enrich phases.
Pod A (:8080) must be running for day_verify.py reranker faithfulness checks.
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
from lib.watchdog.messenger import get_undelivered


def log(msg: str) -> None:
    log_ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{log_ts}] {msg}", flush=True)


def main() -> None:
    t_start = time.monotonic()
    log("=" * 60)
    log("DevForge Day Cycle — extract -> enrich -> verify")
    log("=" * 60)

    preflight_checks("day_cycle.py", required_ports={8080, 8082})

    # Load Watchdog Pulse for injection
    pulses = get_undelivered(target="operator")
    pulse_context = ""
    if pulses:
        pulse_context = "\n### [WATCHDOG PULSE - NEWHAND]\n"
        for p in pulses:
            p_type = p.get("type", "INFO")
            p_content = p.get("content", "")
            pulse_context += f"- [{p_type}] {p_content}\n"
        log(f"  Loaded {len(pulses)} pulses for injection")

    # Phase 1: Extract
    log("\n=== Phase 1: Extract ===")
    t0 = time.monotonic()
    try:
        cmd = [sys.executable, "-u", "extract.py", "--limit", "50",
               "--time-budget", "3600"]
        if pulse_context:
            cmd.extend(["--pulse-context", pulse_context])

        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=3600,
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
        log("  [warn] extract timed out (3600s) — partial results preserved via checkpoint")

    # Phase 2: Enrich (generates tldr, intent, entities, tags)
    log("\n=== Phase 2: Enrich ===")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [sys.executable, "-u", "enrich.py", "--limit", "50",
             "--time-budget", "3600"],
            capture_output=True, text=True, timeout=3600,
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
        log("  [warn] enrich timed out (3600s) — partial results preserved via checkpoint")

    # Phase 3: Verify (reranker on :8080) — entity disk check + faithfulness
    log("\n=== Phase 3: Verify ===")
    t0 = time.monotonic()
    try:
        r = subprocess.run(
            [sys.executable, "-u", "day_verify.py", "--limit", "50"],
            capture_output=True, text=True, timeout=1800,
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
        log("  [warn] verify timed out (1800s) — partial results preserved")

    log(f"\n{'=' * 60}")
    log(f"Day Cycle complete in {time.monotonic() - t_start:.0f}s")
    log(f"{'=' * 60}")


if __name__ == "__main__":
    main()
