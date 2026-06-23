#!/usr/bin/env python3
"""Pipeline E2E test — 10 newest turns, all 5 phases with Pod B mode switching."""
import json, os, subprocess, sys, time

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.pod_manager import ensure_model

NAME = "e2e_10turns_v2"
TEST = test_setup(NAME, "Pipeline E2E: 10 newest turns, ensure_model per phase")

results = {}
overall_start = time.monotonic()

# Phase 1: embed_batch (needs embedder on 8081 — currently running)
log(f"\n{'='*60}\nPHASE: embed_batch\n{'='*60}")
t0 = time.monotonic()
proc = subprocess.run(
    ["python3", "pipelines/embed_batch.py", "--limit", "10"],
    capture_output=True, text=True, timeout=7200,
    env={**os.environ, "PYTHONPATH": SCRIPTS_DIR},
)
elapsed = time.monotonic() - t0
r = results["embed_batch"] = {"returncode": proc.returncode, "elapsed_s": round(elapsed,1), "success": proc.returncode == 0}
log(f"  embed_batch: {'OK' if r['success'] else 'FAIL'} ({elapsed:.0f}s)")
test_heartbeat(f"embed done ({elapsed:.0f}s)")

# Switch Pod B to day-extractor mode (8082) for remaining phases
log(f"\n--- ensure_model(day-extractor) ---")
t_model = time.monotonic()
ensure_model("day-extractor", skip_if_healthy=True)
log(f"  model switch: {time.monotonic()-t_model:.0f}s")
test_heartbeat(f"model switched to day-extractor")

# Phase 2: entity_scan (no LLM needed, but ensure model ready)
log(f"\n{'='*60}\nPHASE: entity_scan\n{'='*60}")
t0 = time.monotonic()
proc = subprocess.run(
    ["python3", "pipelines/entity_scan.py", "--limit", "10"],
    capture_output=True, text=True, timeout=600,
    env={**os.environ, "PYTHONPATH": SCRIPTS_DIR},
)
elapsed = time.monotonic() - t0
r = results["entity_scan"] = {"returncode": proc.returncode, "elapsed_s": round(elapsed,1), "success": proc.returncode == 0}
log(f"  entity_scan: {'OK' if r['success'] else 'FAIL'} ({elapsed:.0f}s)")
test_heartbeat(f"entity_scan done ({elapsed:.0f}s)")

# Phase 3: extract (uses 8082)
log(f"\n{'='*60}\nPHASE: extract\n{'='*60}")
t0 = time.monotonic()
proc = subprocess.run(
    ["python3", "pipelines/extract.py", "--limit", "10"],
    capture_output=True, text=True, timeout=7200,
    env={**os.environ, "PYTHONPATH": SCRIPTS_DIR},
)
elapsed = time.monotonic() - t0
r = results["extract"] = {"returncode": proc.returncode, "elapsed_s": round(elapsed,1), "success": proc.returncode == 0}
log(f"  extract: {'OK' if r['success'] else 'FAIL'} ({elapsed:.0f}s)")
test_heartbeat(f"extract done ({elapsed:.0f}s)")

# Phase 4: enrich (uses 8082 — same model)
log(f"\n{'='*60}\nPHASE: enrich\n{'='*60}")
t0 = time.monotonic()
proc = subprocess.run(
    ["python3", "pipelines/enrich.py", "--limit", "10"],
    capture_output=True, text=True, timeout=7200,
    env={**os.environ, "PYTHONPATH": SCRIPTS_DIR},
)
elapsed = time.monotonic() - t0
r = results["enrich"] = {"returncode": proc.returncode, "elapsed_s": round(elapsed,1), "success": proc.returncode == 0}
log(f"  enrich: {'OK' if r['success'] else 'FAIL'} ({elapsed:.0f}s)")
test_heartbeat(f"enrich done ({elapsed:.0f}s)")

# Phase 5: day_verify (uses 8082 — same model)
log(f"\n{'='*60}\nPHASE: day_verify\n{'='*60}")
t0 = time.monotonic()
proc = subprocess.run(
    ["python3", "pipelines/day_verify.py", "--limit", "10"],
    capture_output=True, text=True, timeout=7200,
    env={**os.environ, "PYTHONPATH": SCRIPTS_DIR},
)
elapsed = time.monotonic() - t0
r = results["day_verify"] = {"returncode": proc.returncode, "elapsed_s": round(elapsed,1), "success": proc.returncode == 0}
log(f"  day_verify: {'OK' if r['success'] else 'FAIL'} ({elapsed:.0f}s)")
test_heartbeat(f"verify done ({elapsed:.0f}s)")

# Summary
total_elapsed = time.monotonic() - overall_start
from lib.db import psql_json
review_facts = psql_json("SELECT fact_type, count(*) as cnt FROM review_facts GROUP BY fact_type ORDER BY fact_type")
embeds = psql_json("SELECT count(*) as cnt FROM embeddings")[0]
entity_turns = psql_json("SELECT count(DISTINCT turn_id) as cnt FROM review_facts WHERE fact_type='entity_scan'")[0]
extract_turns = psql_json("SELECT count(DISTINCT turn_id) as cnt FROM review_facts WHERE fact_type IN ('extract','fact_check')")[0]
verify_turns = psql_json("SELECT count(DISTINCT turn_id) as cnt FROM review_facts WHERE fact_type='verify'")[0]

results["summary"] = {"total_elapsed_s": round(total_elapsed,1), "phases_passed": sum(1 for r in results.values() if r.get("success")), "phases_failed": sum(1 for r in results.values() if not r.get("success"))}
results["db_state"] = {"review_facts_by_type": review_facts, "embeddings_count": embeds["cnt"], "entity_scan_turns": entity_turns["cnt"], "extract_turns": extract_turns["cnt"], "verify_turns": verify_turns["cnt"]}

report_path = f"data/eval/pipeline_e2e_test_{time.strftime('%Y%m%d_%H%M%S')}.json"
os.makedirs("data/eval", exist_ok=True)
with open(report_path, "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)

log(f"\n{'='*60}\nE2E TEST COMPLETE\n  Total: {total_elapsed:.0f}s\n  Passed: {results['summary']['phases_passed']}/5\n  Report: {report_path}\n{'='*60}")
test_complete(f"E2E 10 turns v2: {total_elapsed:.0f}s, {results['summary']['phases_passed']}/5 passed")
