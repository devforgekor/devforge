#!/usr/bin/env python3
# Status: experimental
# Path: none — E2E pipeline test with watchdog supervision
"""E2E Pipeline Test — 6-turn full pipeline with timing, metrics, watchdog supervision.

Runs: embed → entity_scan → extract → enrich → verify (matching day_cycle.sh)
Records: timing per phase, batch metrics, parallel efficiency, long-turn chunking.
Saves structured JSON to data/eval/ for later Claude analysis.

Usage:
  nohup python3 scripts/tests/pipeline_e2e_test.py --limit 6 > /var/tmp/pipeline_test.log 2>&1 &
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINES_DIR = os.path.join(SCRIPTS_DIR, "pipelines")
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)

from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.db import psql_json

KST = timezone(timedelta(hours=9))


def _now_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def _run_phase(cmd: list, label: str, timeout: int = 5400) -> dict:
    """Run a pipeline phase, capture timing, stdout, return code."""
    t0 = time.monotonic()
    rc = -1
    stdout = ""
    stderr = ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = r.returncode
        stdout = r.stdout
        stderr = r.stderr
    except subprocess.TimeoutExpired as e:
        stderr = f"TIMEOUT after {timeout}s"
        if e.stdout:
            stdout = e.stdout
        if e.stderr:
            stderr += "\n" + e.stderr
    except Exception as e:
        stderr = str(e)

    ok = rc == 0
    elapsed = round(time.monotonic() - t0, 1)
    return {
        "phase": label,
        "ok": ok,
        "returncode": rc,
        "elapsed_s": elapsed,
        "stdout": stdout,
        "stderr": stderr,
        "t_start_kst": _now_kst(),
    }


def _query_turn_count(label: str, sql_where: str) -> dict:
    """Query turn count matching a condition."""
    sql = f"SELECT count(*) AS cnt, min(created_at)::text AS oldest, max(created_at)::text AS newest FROM turns t WHERE {sql_where}"
    rows = psql_json(sql) or []
    if rows:
        return {"label": label, "count": rows[0]["cnt"], "oldest": rows[0].get("oldest","?"), "newest": rows[0].get("newest","?")}
    return {"label": label, "count": 0, "oldest": "?", "newest": "?"}


def _db_check() -> dict:
    """Capture DB state for before/after comparison."""
    checks = {}
    for label, where in [
        ("total_turns_with_text", "t.text != ''"),
        ("needs_embed", "t.text != '' AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.source_type='turn' AND e.source_id=t.id)"),
        ("has_embed", "EXISTS (SELECT 1 FROM embeddings e WHERE e.source_type='turn' AND e.source_id=t.id)"),
        ("needs_entity_scan", "t.text != '' AND NOT EXISTS (SELECT 1 FROM review_facts rf WHERE rf.turn_id=t.id AND rf.fact_type='entity_scan')"),
        ("needs_extract", "EXISTS (SELECT 1 FROM review_facts rf1 WHERE rf1.turn_id=t.id AND rf1.fact_type='entity_scan') AND NOT EXISTS (SELECT 1 FROM review_facts rf2 WHERE rf2.turn_id=t.id AND rf2.fact_type='text')"),
        ("needs_enrich", "EXISTS (SELECT 1 FROM review_facts rf1 WHERE rf1.turn_id=t.id AND rf1.fact_type='text') AND NOT EXISTS (SELECT 1 FROM review_facts rf2 WHERE rf2.turn_id=t.id AND rf2.fact_type='enrich_meta')"),
        ("needs_verify", "EXISTS (SELECT 1 FROM review_facts rf1 WHERE rf1.turn_id=t.id AND rf1.fact_type='enrich_meta') AND NOT EXISTS (SELECT 1 FROM review_facts rf2 WHERE rf2.turn_id=t.id AND rf2.fact_type='verify_result')"),
        ("large_turns_5k", "char_length(t.text) > 5000"),
    ]:
        result = _query_turn_count(label, where)
        checks[label] = result
    return checks


def _check_heartbeat(name: str) -> dict:
    """Check if a pipeline heartbeat exists and is fresh."""
    rows = psql_json(
        f"SELECT status, created_at::text, instruction "
        f"FROM watchdog_pulses WHERE pulse_id = 'heartbeat_{name}'"
    ) or []
    if rows:
        return {"worker": name, "exists": True, "status": rows[0].get("status","?"), "created_at": rows[0].get("created_at","?"), "instruction": rows[0].get("instruction","")[:80]}
    return {"worker": name, "exists": False}


def main():
    parser = argparse.ArgumentParser(description="E2E Pipeline Test")
    parser.add_argument("--limit", type=int, default=6, help="Turns per phase")
    args = parser.parse_args()

    limit = args.limit
    phases_log = []
    db_before = {}
    db_after = {}
    heartbeat_status = {}
    results_path = os.path.join(EVAL_DIR, f"pipeline_e2e_test_{datetime.now(KST).strftime('%Y%m%d_%H%M%S')}.json")

    # ── Setup ──
    TEST = test_setup("pipeline_e2e", f"E2E pipeline test (limit={limit})")
    log(f"[pipeline_e2e] Results: {results_path}")
    log(f"[pipeline_e2e] Watching: oscar the watchdog")

    # ── DB Before ──
    test_heartbeat("db_snapshot_before")
    db_before = _db_check()
    log(f"[pipeline_e2e] DB before: {json.dumps({k: v['count'] for k, v in sorted(db_before.items())}, indent=2)}")

    # ── Phase 1: Embed ──
    test_heartbeat("phase1_embed")
    log(f"[pipeline_e2e] Phase 1: embed_batch (limit={limit})")
    # embed model is already on :8081 from pre-test load
    r = _run_phase(["python3", os.path.join(PIPELINES_DIR, "embed_batch.py"), f"--limit", str(limit)], "embed")
    phases_log.append(r)
    log(f"  embed: {'OK' if r['ok'] else 'FAIL'} ({r['elapsed_s']}s)")
    # Check heartbeat was created
    heartbeat_status["embed"] = _check_heartbeat("embed_batch")
    log(f"  embed heartbeat: {heartbeat_status['embed']}")

    # ── Phase 2: Entity Scan ──
    test_heartbeat("phase2_entity_scan")
    log(f"[pipeline_e2e] Phase 2: entity_scan (limit={limit})")
    r = _run_phase(["python3", os.path.join(PIPELINES_DIR, "entity_scan.py"), f"--limit", str(limit)], "entity_scan")
    phases_log.append(r)
    log(f"  entity_scan: {'OK' if r['ok'] else 'FAIL'} ({r['elapsed_s']}s)")
    heartbeat_status["entity_scan"] = _check_heartbeat("entity_scan")

    # ── Phase 3: Extract (switch inference to day-extractor on :8082) ──
    test_heartbeat("phase3_model_switch_extract")
    log(f"[pipeline_e2e] Phase 3: switch to day-extractor (:8082), then extract...")
    # Switch model: embed(:8081) -> day-extractor(:8082)
    try:
        from lib.pod_manager import ensure_model
        sw_ok = ensure_model("day-extractor", skip_if_healthy=True)
        log(f"  model switch day-extractor: {'OK' if sw_ok else 'FAIL'}")
    except Exception as e:
        log(f"  model switch FAILED: {e}")
        sw_ok = False

    if sw_ok:
        test_heartbeat("phase3_extract")
        r = _run_phase(["python3", os.path.join(PIPELINES_DIR, "extract.py"), f"--limit", str(limit)], "extract")
        phases_log.append(r)
        log(f"  extract: {'OK' if r['ok'] else 'FAIL'} ({r['elapsed_s']}s)")
        heartbeat_status["day_extract"] = _check_heartbeat("day_extract")
    else:
        log(f"  extract: SKIPPED (model not available)")
        phases_log.append({"phase": "extract", "ok": False, "skipped": True, "reason": "model_switch_failed"})

    # ── Phase 4: Enrich (same day-extractor model on :8082) ──
    test_heartbeat("phase4_enrich")
    log(f"[pipeline_e2e] Phase 4: enrich (same model)" )
    r = _run_phase(["python3", os.path.join(PIPELINES_DIR, "enrich.py"), f"--limit", str(limit)], "enrich")
    phases_log.append(r)
    log(f"  enrich: {'OK' if r['ok'] else 'FAIL'} ({r['elapsed_s']}s)")
    heartbeat_status["day_enrich"] = _check_heartbeat("day_enrich")

    # ── Phase 5: Verify (swap to day-verifier on :8082) ──
    test_heartbeat("phase5_model_switch_verify")
    log(f"[pipeline_e2e] Phase 5: switch to day-verifier (:8082), then verify...")
    try:
        sw_ok = ensure_model("day-verifier", skip_if_healthy=True)
        log(f"  model switch day-verifier: {'OK' if sw_ok else 'FAIL'}")
    except Exception as e:
        log(f"  model switch FAILED: {e}")
        sw_ok = False

    if sw_ok:
        test_heartbeat("phase5_verify")
        r = _run_phase(["python3", os.path.join(PIPELINES_DIR, "day_verify.py"), f"--limit", str(limit)], "verify")
        phases_log.append(r)
        log(f"  verify: {'OK' if r['ok'] else 'FAIL'} ({r['elapsed_s']}s)")
        heartbeat_status["day_verify"] = _check_heartbeat("day_verify")
    else:
        log(f"  verify: SKIPPED (model not available)")
        phases_log.append({"phase": "verify", "ok": False, "skipped": True, "reason": "model_switch_failed"})

    # ── DB After ──
    test_heartbeat("db_snapshot_after")
    db_after = _db_check()

    # ── Summary ──
    test_heartbeat("building_report")
    total_elapsed = sum(p.get("elapsed_s", 0) for p in phases_log if p.get("elapsed_s"))
    model_switch_elapsed = 0  # not tracked per-switch

    summary = {
        "test_name": "pipeline_e2e",
        "t_start_kst": _now_kst(),
        "limit": limit,
        "phases": phases_log,
        "total_elapsed_s": total_elapsed,
        "heartbeat_status": heartbeat_status,
        "db_before": {k: v["count"] for k, v in sorted(db_before.items())},
        "db_after": {k: v["count"] for k, v in sorted(db_after.items())},
        "db_delta": {k: db_after[k]["count"] - v["count"] for k, v in db_before.items()},
        "errors": [p for p in phases_log if not p.get("ok")],
    }

    with open(results_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)

    log(f"[pipeline_e2e] Report saved: {results_path}")
    log(f"[pipeline_e2e] Total pipeline time: {total_elapsed}s")
    log(f"[pipeline_e2e] Phases: {', '.join(p['phase'] for p in phases_log)}")
    log(f"[pipeline_e2e] Errors: {len(summary['errors'])}")

    # ── Extract key metrics from stdout for analysis ──
    for p in phases_log:
        phase = p["phase"]
        stdout = p.get("stdout", "")
        if "embed" in phase:
            for line in stdout.split("\n"):
                if "batches" in line.lower() and "max" not in line.lower():
                    summary.setdefault("metrics", {})["embed_batches"] = line.strip()
                if "Done" in line and "embedded" in line.lower():
                    summary.setdefault("metrics", {})["embed_summary"] = line.strip()
        if "extract" in phase:
            for line in stdout.split("\n"):
                if "facts" in line.lower() and "stored" in line.lower():
                    summary.setdefault("metrics", {}).setdefault("extract_facts", []).append(line.strip())
                if "Done" in line:
                    summary.setdefault("metrics", {})["extract_done"] = line.strip()
        if "enrich" in phase:
            for line in stdout.split("\n"):
                if "Done" in line and "enriched" in line.lower():
                    summary.setdefault("metrics", {})["enrich_done"] = line.strip()
        if "verify" in phase:
            for line in stdout.split("\n"):
                if "Done" in line and "verified" in line.lower():
                    summary.setdefault("metrics", {})["verify_done"] = line.strip()
                if "faithfulness" in line:
                    summary.setdefault("metrics", {}).setdefault("verify_faithfulness", []).append(line.strip())

    with open(results_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
    log(f"[pipeline_e2e] Final report with metrics: {results_path}")

    # ── Cleanup ──
    test_complete("pipeline_e2e test complete")
    log(f"[pipeline_e2e] Done. All phases complete in {total_elapsed}s")


if __name__ == "__main__":
    main()
