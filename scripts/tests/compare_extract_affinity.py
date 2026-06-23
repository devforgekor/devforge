#!/usr/bin/env python3
# Status: experimental
# Path: manual — extract affinity config comparison
"""Compare extract affinity configs: parallel/threads/cpus.

Measures wall-clock time, facts extracted, and retry/error count
for 3 configurations on identical 2-turn extract dry-run.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.pod_manager import ensure_model, MODEL_METADATA

TEST = test_setup("extract_affinity", "Compare extract affinity configs: parallel/threads/cpus")

CONFIGS = [
    {"name": "A baseline", "parallel": 2, "threads": 4, "cpus": None},
    {"name": "B cpus=0-2 p=1 t=3", "parallel": 1, "threads": 3, "cpus": "0-2"},
    {"name": "C cpus=0-3 p=1 t=4", "parallel": 1, "threads": 4, "cpus": "0-3"},
]

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXTRACT_PY = os.path.join(PROJECT_DIR, "scripts", "pipelines", "extract.py")

results = []

for cfg in CONFIGS:
    log(f"\n{'=' * 60}")
    log(f"Config: {cfg['name']}")
    log(f"{'=' * 60}")

    meta = MODEL_METADATA["day-extractor"]
    meta["parallel"] = cfg["parallel"]
    meta["threads"] = cfg["threads"]
    meta["threads_batch"] = cfg["threads"]
    if cfg["cpus"]:
        meta["cpus"] = cfg["cpus"]
    else:
        meta.pop("cpus", None)

    # Force restart with new params
    t0 = time.monotonic()
    log(f"  Restarting Pod B with {cfg}...")
    ensure_model("day-extractor", skip_if_healthy=False)
    restart_elapsed = time.monotonic() - t0
    log(f"  Pod restart: {restart_elapsed:.0f}s")

    test_heartbeat(f"Running {cfg['name']}...")

    # Run extract --dry-run --limit 2 with matching --parallel
    env = os.environ.copy()
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, EXTRACT_PY, "--dry-run", "--limit", "2",
             "--json", "--parallel", str(cfg["parallel"])],
            capture_output=True, text=True, timeout=7200,
            cwd=PROJECT_DIR, env=env,
        )
    except subprocess.TimeoutExpired:
        log(f"  TIMEOUT after 7200s")
        results.append({"config": cfg["name"], "elapsed_s": 7200, "facts": 0,
                        "processed": 0, "failed": 2, "retries": -1,
                        "restart_s": round(restart_elapsed), "ok": False, "error": "timeout"})
        continue

    elapsed = time.monotonic() - t0
    log(f"  Extract wall-clock: {elapsed:.0f}s")

    # Parse JSON result (last JSON line in stdout)
    result = None
    for line in reversed(proc.stdout.strip().split("\n")):
        line = line.strip()
        if line:
            try:
                result = json.loads(line)
                log(f"  JSON result: {json.dumps(result)}")
                break
            except json.JSONDecodeError:
                continue

    if result is None:
        log(f"  WARNING: no JSON result found. Dumping last 20 lines of stdout:")
        for line in proc.stdout.strip().split("\n")[-20:]:
            log(f"    | {line}")
        log(f"  STDERR last 10 lines:")
        for line in proc.stderr.strip().split("\n")[-10:]:
            log(f"    | {line}")

    # Count retries: lines matching "attempt X/Y failed"
    combined = proc.stdout + proc.stderr
    retry_count = sum(1 for line in combined.split("\n") if "attempt" in line and "failed" in line)
    error_count = sum(1 for line in combined.split("\n") if "ERROR" in line.upper() and "attempt" not in line.lower())

    out = {
        "config": cfg["name"],
        "elapsed_s": round(elapsed),
        "processed": result.get("processed", 0) if result else 0,
        "failed": result.get("failed", 0) if result else 0,
        "facts": result.get("facts", 0) if result else 0,
        "ok": result.get("ok", False) if result else False,
        "retries": retry_count,
        "errors": error_count,
        "restart_s": round(restart_elapsed),
    }
    results.append(out)
    log(f"  Result: elapsed={out['elapsed_s']}s facts={out['facts']} proc={out['processed']} fail={out['failed']} retry={out['retries']} error={out['errors']}")

    if not out["ok"]:
        log(f"  Config failed! stdout tail:")
        for line in proc.stdout.strip().split("\n")[-15:]:
            log(f"    {line}")

# Summary table
log(f"\n{'=' * 60}")
log(f"COMPARISON SUMMARY")
log(f"{'=' * 60}")
log(f"{'Config':<28} {'Time(s)':>8} {'Facts':>6} {'Proc':>5} {'Fail':>5} {'Retry':>6} {'Err':>5} {'Restart':>8}")
log(f"{'-'*28} {'-'*8} {'-'*6} {'-'*5} {'-'*5} {'-'*6} {'-'*5} {'-'*8}")
for r in results:
    log(f"{r['config']:<28} {r['elapsed_s']:>8} {r['facts']:>6} {r['processed']:>5} {r['failed']:>5} {r['retries']:>6} {r['errors']:>5} {r['restart_s']:>8}")

test_complete("all configs compared")
