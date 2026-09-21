#!/usr/bin/env python3
# Status: experimental
# Path: manual — parallel=2 affinity comparison on OOM turns
"""Compare parallel=2 affinity configs on 3 previously-failed turns.

Config A: parallel=2, threads=3, cpus=0-2
Config B: parallel=2, threads=2, cpus=0-2
Config C: parallel=2, threads=4, cpus=0-2

Uses --turn-id for each of the 3 OOM turns.
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.pod_manager import MODEL_METADATA, ensure_model
from lib.test_common import log, test_complete, test_heartbeat, test_setup

# 3 failed turns from DB (largest → smallest)
TURN_IDS = [
    "2cd9e45f-df54-41a8-aef1-34cfbd9217d4",  # 502+1572+325 = 2399ch
    "c2893937-7eda-4172-91f7-e73f51ec6666",  # 151+1916+312 = 2379ch
    "980601c8-9c81-41fd-95ca-d5f4283a005a",  # 69+2151+83 = 2303ch
]

CONFIGS = [
    {"name": "A p=2 t=3 cpus=0-2", "parallel": 2, "threads": 3, "cpus": "0-2"},
    {"name": "B p=2 t=2 cpus=0-2", "parallel": 2, "threads": 2, "cpus": "0-2"},
    {"name": "C p=2 t=4 cpus=0-2", "parallel": 2, "threads": 4, "cpus": "0-2"},
    {"name": "D p=2 t=4 cpus=0-3", "parallel": 2, "threads": 4, "cpus": "0-3"},
]

TEST = test_setup("extract_oom_3turns", "Compare 3 affinity configs on OOM turns")

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
    ensure_model("day-extractor", skip_if_healthy=False)
    restart_s = round(time.monotonic() - t0)
    log(f"  Pod restart: {restart_s}s")

    cfg_facts = 0
    cfg_processed = 0
    cfg_failed = 0
    cfg_retries = 0
    cfg_elapsed = 0

    for tid in TURN_IDS:
        test_heartbeat(f"{cfg['name']} — turn {tid[:8]}...")
        t0 = time.monotonic()
        result = subprocess.run(
            [
                sys.executable,
                EXTRACT_PY,
                "--dry-run",
                "--turn-id",
                tid,
                "--json",
                "--parallel",
                str(cfg["parallel"]),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
            cwd=PROJECT_DIR,
        )
        elapsed = round(time.monotonic() - t0)

        # Parse JSON from last lines (extract.py outputs indented multi-line JSON)
        d = {}
        stdout = result.stdout.strip()
        brace_start = stdout.rfind("{\n")
        if brace_start >= 0:
            json_str = stdout[brace_start:]
            try:
                d = json.loads(json_str)
            except json.JSONDecodeError:
                pass

        ok = d.get("ok", False)
        facts = d.get("facts", 0)
        retries = sum(
            1
            for l in (result.stdout + result.stderr).split("\n")
            if "attempt" in l and "failed" in l
        )
        cfg_facts += facts
        cfg_processed += d.get("processed", 0)
        cfg_failed += d.get("failed", 0)
        cfg_retries += retries
        cfg_elapsed += elapsed
        log(f"    turn {tid[:8]}: {elapsed}s, facts={facts}, ok={ok}, retry={retries}")

        # Brief cooldown between turns
        time.sleep(3)

    results.append(
        {
            "config": cfg["name"],
            "elapsed_s": cfg_elapsed,
            "facts": cfg_facts,
            "processed": cfg_processed,
            "failed": cfg_failed,
            "retries": cfg_retries,
            "restart_s": restart_s,
        }
    )
    log(
        f"  Subtotal: {cfg_elapsed}s, facts={cfg_facts}, processed={cfg_processed}, fail={cfg_failed}, retry={cfg_retries}"
    )

# Summary
log(f"\n{'=' * 60}")
log("COMPARISON SUMMARY (3 failed turns)")
log(f"{'=' * 60}")
log(f"{'Config':<22} {'Time(s)':>8} {'Facts':>6} {'Proc':>5} {'Fail':>5} {'Retry':>6}")
log(f"{'-' * 22} {'-' * 8} {'-' * 6} {'-' * 5} {'-' * 5} {'-' * 6}")
for r in results:
    log(
        f"{r['config']:<22} {r['elapsed_s']:>8} {r['facts']:>6} {r['processed']:>5} "
        f"{r['failed']:>5} {r['retries']:>6}"
    )

test_complete("3 configs compared on 3 OOM turns")
