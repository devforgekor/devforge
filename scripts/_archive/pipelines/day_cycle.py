#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Day cycle phase runner (extract → enrich → verify)
"""Day-time chain: sequential phase execution for entity_scan → extract → enrich → verify.

Each phase runs with its own token-based batch limit (internal --limit).
No time-budget slicing or MAX_BUDGET — each phase processes available backlog
via state-based filters (IS NULL / NOT EXISTS). The 55-min overlap guard is
handled by day_cycle.sh systemd timer, not this script.

Phase order:
  0. Entity Scan (no LLM) — deterministic regex+DB entity extraction for context
  1. Extract (extract model :8082) — fact extraction + NLI+reranker verify
  2. Enrich (extract model :8082) — fields generation + substring+reranker entity verify
  3. Verify (verify model :8082, after model swap) — independent LLM verify + reranker check

Swap-based: extract/enrich on :8082 (extract model), then swap to verify on :8082 (verify model).
Polish + FTS5 + embedding are handled by day_cycle.sh before this script.

NLI→reranker pattern (both extract + enrich):
  1st pass: LLM NLI (logical entailment) — ENTAILMENT→grounded, CONTRADICTION→drop
  2nd pass: Reranker (topical relevance via Pod A :8080) — only NEUTRAL/uncertain items
"""

import os
import subprocess
import sys
import time
from typing import List, Optional, Tuple, Union

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(os.path.join(SCRIPTS_DIR, "pipelines"))

from lib.infra.preflight import preflight_checks

# Phase entry: (name, script, [port])
# port=None means preflight without port check
PhaseEntry = Tuple[str, str, Optional[int]]

PHASES: List[PhaseEntry] = [
    ("entity_scan", "entity_scan.py", None),   # Phase 0: no LLM, no port
    ("extract", "extract.py", 8082),
    ("enrich", "enrich.py", 8082),
]


# ── Phase runner ─────────────────────────────────────────────────────────

def _run_phase(name: str, script: str,
               preflight_port: Optional[int] = 8082) -> bool:
    """Run a pipeline phase. Each script self-limits via --limit."""
    if preflight_port is not None:
        preflight_checks(script, required_ports={preflight_port})
    else:
        preflight_checks(script)
    cmd = [sys.executable, "-u", script]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        elapsed = time.monotonic() - t0
        ok = r.returncode == 0
        status = "ok" if ok else f"warn (exit={r.returncode})"
        printed = 0
        for line in r.stdout.splitlines():
            if printed < 5:
                print(f"    {line}", flush=True)
                printed += 1
        if printed >= 5:
            remaining = len(r.stdout.splitlines()) - 5
            print(f"    ... ({remaining} more lines)", flush=True)
        if r.stderr:
            for line in r.stderr.splitlines()[:3]:
                print(f"    [stderr] {line}", flush=True)
        print(f"  [{status}] {name}: {elapsed:.0f}s", flush=True)
        return ok
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        print(f"  [timeout] {name}: {elapsed:.0f}s", flush=True)
        return False


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    t_start = time.monotonic()

    print(f"\n{'=' * 60}", flush=True)
    print("DevForge Day Cycle — extract → enrich (:8082, verify runs after model swap)", flush=True)
    print(f"{'=' * 60}", flush=True)
    print("  Mode: extract model :8082 (extract/enrich), verify uses separate model swap", flush=True)

    preflight_checks("day_cycle.py", required_ports={8082})

    for phase_name, script, port in PHASES:
        port_str = f":{port}" if port else ""
        print(f"\n  === {phase_name} ({script}{port_str}) ===", flush=True)
        ok = _run_phase(phase_name, script, preflight_port=port)
        if not ok:
            print(f"  [warn] {phase_name} failed — continuing", flush=True)

    total_elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}", flush=True)
    print(f"Day Cycle complete in {total_elapsed}s", flush=True)
    print(f"{'=' * 60}", flush=True)


if __name__ == "__main__":
    main()
