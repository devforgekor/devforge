#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""E2E Pipeline Test — section-split extract.

Runs: embed_batch → entity_scan → extract → enrich → day_verify
Auto-recovers 8082 on crash. Registers test protection. Saves snapshots per phase.
"""
import json, os, sys, time, traceback

SCRIPTS_DIR = "/opt/projects/server/scripts"
os.chdir(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from lib.test_common import test_setup, test_heartbeat, test_complete
from lib.common import log
from lib.pod_manager import ensure_model
from lib.db import psql_json

BATCH_LIMIT = 10
SNAPSHOT_DIR = "data/eval"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)
TS = time.strftime("%Y%m%d_%H%M%S")

snapshots = {}

def save_snapshot(phase, data):
    path = f"{SNAPSHOT_DIR}/e2e_{phase}_{TS}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    log(f"  Snapshot saved: {path}")
    return path

def call_with_recovery(fn, phase_name, max_retries=3):
    """Call fn, auto-recover 8082 on crash."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            err = str(e)
            log(f"  [{phase_name}] attempt {attempt} failed: {type(e).__name__}: {err[:120]}")
            if attempt < max_retries:
                log(f"  [{phase_name}] recovering 8082...")
                ensure_model('day-extractor', skip_if_healthy=False)
                log(f"  [{phase_name}] 8082 recovered, retrying...")
                test_heartbeat(f"{phase_name} retry {attempt}")
            else:
                raise

def run_embed():
    log("=" * 60)
    log("Phase 1: embed_batch")
    log("=" * 60)
    from pipelines.embed_batch import main as embed_main
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(["--json"])
    t0 = time.monotonic()
    call_with_recovery(lambda: embed_main(args), "embed_batch")
    elapsed = time.monotonic() - t0
    # Check results
    rows = psql_json(
        "SELECT count(*) AS cnt FROM embeddings "
        "WHERE created_at > now() - interval '1 hour'"
    ) or [{"cnt": 0}]
    cnt = rows[0]["cnt"] if rows else 0
    result = {"count": cnt, "elapsed_s": round(elapsed, 1)}
    log(f"  embedded {cnt} turns in {elapsed:.0f}s")
    path = save_snapshot("embed_batch", result)
    snapshots["embed_batch"] = {"path": path, "data": result}
    return result

def run_entity_scan():
    log("=" * 60)
    log("Phase 2: entity_scan")
    log("=" * 60)
    from pipelines.entity_scan import main as escan_main
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    args = parser.parse_args([f"--limit={BATCH_LIMIT}"])
    t0 = time.monotonic()
    call_with_recovery(lambda: escan_main(args), "entity_scan")
    elapsed = time.monotonic() - t0
    result = {"elapsed_s": round(elapsed, 1)}
    log(f"  entity_scan done in {elapsed:.0f}s")
    path = save_snapshot("entity_scan", result)
    snapshots["entity_scan"] = {"path": path, "data": result}
    return result

def run_extract():
    log("=" * 60)
    log("Phase 3: extract (section-split)")
    log("=" * 60)
    from pipelines.extract import main as extract_main
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    args = parser.parse_args([f"--limit={BATCH_LIMIT}"])
    t0 = time.monotonic()
    call_with_recovery(lambda: extract_main(args), "extract")
    elapsed = time.monotonic() - t0
    # Check results
    rows = psql_json(
        "SELECT fact_type, count(*) AS cnt FROM review_facts "
        "WHERE source = 'extract_pipeline' "
        "AND created_at > now() - interval '2 hours' "
        "GROUP BY fact_type ORDER BY fact_type"
    ) or []
    by_type = {r["fact_type"]: r["cnt"] for r in rows}
    total = sum(by_type.values())
    result = {"by_type": by_type, "total": total, "elapsed_s": round(elapsed, 1)}
    log(f"  extracted {total} facts ({by_type}) in {elapsed:.0f}s")
    path = save_snapshot("extract", result)
    snapshots["extract"] = {"path": path, "data": result}
    return result

def run_enrich():
    log("=" * 60)
    log("Phase 4: enrich (NLI + TLDR)")
    log("=" * 60)
    from pipelines.enrich import main as enrich_main
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    args = parser.parse_args([f"--limit={BATCH_LIMIT}"])
    t0 = time.monotonic()
    call_with_recovery(lambda: enrich_main(args), "enrich")
    elapsed = time.monotonic() - t0
    rows = psql_json(
        "SELECT result, count(*) AS cnt FROM review_facts "
        "WHERE source = 'enrich_pipeline' "
        "AND created_at > now() - interval '2 hours' "
        "GROUP BY result ORDER BY result"
    ) or []
    by_result = {r["result"]: r["cnt"] for r in rows}
    total = sum(by_result.values())
    result = {"by_result": by_result, "total": total, "elapsed_s": round(elapsed, 1)}
    log(f"  enriched {total} facts ({by_result}) in {elapsed:.0f}s")
    path = save_snapshot("enrich", result)
    snapshots["enrich"] = {"path": path, "data": result}
    return result

def run_day_verify():
    log("=" * 60)
    log("Phase 5: day_verify")
    log("=" * 60)
    from pipelines.day_verify import main as verify_main
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    args = parser.parse_args([f"--limit={BATCH_LIMIT}"])
    t0 = time.monotonic()
    call_with_recovery(lambda: verify_main(args), "day_verify")
    elapsed = time.monotonic() - t0
    result = {"elapsed_s": round(elapsed, 1)}
    log(f"  day_verify done in {elapsed:.0f}s")
    path = save_snapshot("day_verify", result)
    snapshots["day_verify"] = {"path": path, "data": result}
    return result

def main():
    log("=" * 70)
    log("E2E PIPELINE TEST — section-split extract")
    log(f"  BATCH_LIMIT={BATCH_LIMIT}, watchdog active")
    log("=" * 70)
    log(f"Started at: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")

    # Register test protection
    test_setup("e2e_extract_section_split",
               "E2E test: section-split extract pipeline with 10-turn batch")

    try:
        phases = [
            ("embed_batch", run_embed),
            ("entity_scan", run_entity_scan),
            ("extract", run_extract),
            ("enrich", run_enrich),
            ("day_verify", run_day_verify),
        ]

        for phase_name, phase_fn in phases:
            test_heartbeat(f"phase:{phase_name}")
            try:
                phase_fn()
            except Exception as e:
                log(f"[FATAL] {phase_name} failed: {type(e).__name__}: {e}")
                # Mark the phase as failed and continue to next
                snapshots[phase_name] = {
                    "error": f"{type(e).__name__}: {str(e)[:200]}"
                }
                # If extract fails, the rest doesn't make sense
                if phase_name == "extract":
                    log("  extract failed — cannot proceed to enrich/verify without facts")
                    break

        # Save full report
        full_report = {
            "meta": {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "batch_limit": BATCH_LIMIT,
                "test": "E2E section-split extract pipeline",
            },
            "snapshots": snapshots,
        }
        path = save_snapshot("full_report", full_report)
        log(f"\nFull E2E report: {path}")

    finally:
        test_complete(f"E2E test {'completed' if snapshots.get('day_verify') else 'partial'}")
        log("E2E test cleanup done.")

if __name__ == "__main__":
    main()
