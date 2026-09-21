#!/usr/bin/env python3
# Status: experimental
# Path: manual — parallel=2 affinity comparison (6 turns)
"""Compare parallel=2 with different thread/cpus configs.

Config A: parallel=2, threads=3, cpus=0-2
Config B: parallel=2, threads=2, cpus=0-2

Both run --limit 6 --dry-run.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.pod_manager import MODEL_METADATA, ensure_model
from lib.test_common import log, test_complete, test_heartbeat, test_setup

TEST = test_setup("extract_affinity_6t", "Compare parallel=2 affinity for 6 turns")

CONFIGS = [
    {"name": "A p=2 t=3 cpus=0-2", "parallel": 2, "threads": 3, "cpus": "0-2"},
    {"name": "B p=2 t=2 cpus=0-2", "parallel": 2, "threads": 2, "cpus": "0-2"},
]

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXTRACT_PY = os.path.join(PROJECT_DIR, "scripts", "pipelines", "extract.py")
results = []

for cfg in CONFIGS:
    log(f"\n{'=' * 60}")
    log(f"Config: {cfg['name']}")
    log(f"{'=' * 60}")

    meta = copy.deepcopy(MODEL_METADATA.get("day-extractor", {}))
    meta["parallel"] = cfg["parallel"]
    meta["threads"] = cfg["threads"]
    meta["threads_batch"] = cfg["threads"]
    meta["cpus"] = cfg["cpus"]

    t0 = time.monotonic()
    log("  Restarting inference...")
    ensure_model("day-extractor", skip_if_healthy=False)
    restart_s = round(time.monotonic() - t0)
    log(f"  Pod restart: {restart_s}s")

    test_heartbeat(f"Running {cfg['name']}...")

    t0 = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            EXTRACT_PY,
            "--dry-run",
            "--limit",
            "6",
            "--json",
            "--parallel",
            str(cfg["parallel"]),
        ],
        capture_output=True,
        text=True,
        timeout=10800,
        cwd=PROJECT_DIR,
    )
    elapsed_s = round(time.monotonic() - t0)
    log(f"  Extract wall-clock: {elapsed_s}s")

    out = {"config": cfg["name"], "elapsed_s": elapsed_s, "restart_s": restart_s}
    for line in reversed(result.stdout.strip().split("\n")):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
                out.update(d)
            except json.JSONDecodeError:
                pass
            break

    retries = sum(
        1 for l in (result.stdout + result.stderr).split("\n") if "attempt" in l and "failed" in l
    )
    out["retries"] = retries
    out["facts_per_turn"] = round(out.get("facts", 0) / max(out.get("processed", 1), 1), 1)

    results.append(out)
    log(
        f"  Result: {out.get('elapsed_s', '?')}s, facts={out.get('facts', '?')}, "
        f"proc={out.get('processed', '?')}, fail={out.get('failed', '?')}, "
        f"retry={retries}, facts/turn={out.get('facts_per_turn', '?')}"
    )

# Summary
log(f"\n{'=' * 60}")
log("COMPARISON SUMMARY (6 turns)")
log(f"{'=' * 60}")
log(f"{'Config':<25} {'Time(s)':>8} {'Facts':>6} {'Proc':>5} {'Fail':>5} {'Retry':>6} {'F/T':>5}")
log(f"{'-' * 25} {'-' * 8} {'-' * 6} {'-' * 5} {'-' * 5} {'-' * 6} {'-' * 5}")
for r in results:
    log(
        f"{r['config']:<25} {r['elapsed_s']:>8} {r['facts']:>6} "
        f"{r['processed']:>5} {r['failed']:>5} {r['retries']:>6} "
        f"{r['facts_per_turn']:>5}"
    )

test_complete("both configs compared")
