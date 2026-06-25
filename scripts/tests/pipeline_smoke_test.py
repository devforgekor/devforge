#!/usr/bin/env python3
# Status: experimental
# Path: manual — full pipeline smoke test with new affinity config
"""Full pipeline smoke test: extract → enrich → day_verify with --limit 2 --dry-run.

Validates the new affinity config (cpus=0-2, threads=3, parallel=1, temp=0.0).
"""
import os, sys, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.pod_manager import ensure_model

TEST = test_setup("pipeline_smoke", "Full pipeline smoke test with affinity config")

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PIPELINES = os.path.join(PROJECT_DIR, "scripts", "pipelines")
results = []
ok_count = 0

for phase, model_key in [("extract", "day-extractor"), ("enrich", "day-enricher"), ("day_verify", "day-verifier")]:
    log(f"\n{'=' * 60}")
    log(f"Phase: {phase} ({model_key})")
    log(f"{'=' * 60}")

    t0 = time.monotonic()
    ensure_model(model_key, skip_if_healthy=False)
    log(f"  Model load: {time.monotonic()-t0:.0f}s")

    test_heartbeat(f"Running {phase}...")
    t0 = time.monotonic()
    py = f"scripts/pipelines/{phase}.py"
    proc = os.system(
        f"cd {PROJECT_DIR} && PYTHONPATH=scripts python3 {py} --dry-run --limit 2 2>&1"
    )
    elapsed = time.monotonic() - t0
    ok = proc == 0
    results.append({"phase": phase, "elapsed_s": round(elapsed), "ok": ok})
    if ok:
        ok_count += 1
    log(f"  {phase}: {elapsed:.0f}s {'OK' if ok else 'FAIL'}")

log(f"\n{'=' * 60}")
log(f"PIPELINE SMOKE TEST SUMMARY: {ok_count}/3 phases OK")
for r in results:
    log(f"  {r['phase']:<20} {r['elapsed_s']:>6}s {'✅' if r['ok'] else '❌'}")
log(f"{'=' * 60}")

test_complete(f"{ok_count}/3 phases OK")
