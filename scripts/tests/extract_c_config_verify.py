#!/usr/bin/env python3
# Status: experimental
# Path: manual — quality verification for Config C
"""Extract 3 previously-failed OOM turns with Config C, store & verify quality.

Config C: parallel=2, threads=4, cpus=0-2, temp=0.0
"""
import os, sys, time, json, subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.pod_manager import ensure_model, MODEL_METADATA

TURN_IDS = [
    "2cd9e45f-df54-41a8-aef1-34cfbd9217d4",
    "c2893937-7eda-4172-91f7-e73f51ec6666",
    "980601c8-9c81-41fd-95ca-d5f4283a005a",
]

TEST = test_setup("extract_c_config_verify", "Real extract with Config C — quality verification")

PROJECT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXTRACT_PY = os.path.join(PROJECT_DIR, "scripts", "pipelines", "extract.py")

# Apply Config C to day-extractor metadata
meta = MODEL_METADATA["day-extractor"]
meta["parallel"] = 2
meta["threads"] = 4
meta["threads_batch"] = 4
meta["cpus"] = "0-2"

log(f"Config C: parallel={meta['parallel']}, threads={meta['threads']}, cpus={meta.get('cpus')}")

t0 = time.monotonic()
ensure_model("day-extractor", skip_if_healthy=False)
log(f"Pod restart: {time.monotonic()-t0:.0f}s")

for tid in TURN_IDS:
    test_heartbeat(f"Extracting turn {tid[:8]}...")
    t0 = time.monotonic()
    result = subprocess.run(
        [sys.executable, EXTRACT_PY, "--turn-id", tid,
         "--json", "--parallel", "2"],
        capture_output=True, text=True, timeout=3600,
        cwd=PROJECT_DIR,
    )
    elapsed = time.monotonic() - t0

    # Parse output
    stdout = result.stdout.strip()
    brace = stdout.rfind("{\n")
    d = {}
    if brace >= 0:
        try: d = json.loads(stdout[brace:])
        except json.JSONDecodeError: pass

    ok = d.get("ok", False)
    facts = d.get("facts", 0)
    log(f"  turn {tid[:8]}: {elapsed:.0f}s, facts={facts}, ok={ok}")
    if not ok:
        for line in stdout.split("\n")[-10:]:
            log(f"    | {line}")

    time.sleep(3)

# Query quality from DB
log(f"\n{'=' * 60}")
log(f"QUALITY CHECK — review_facts from Config C")
log(f"{'=' * 60}")
for tid in TURN_IDS:
    db = subprocess.run(
        ["podman", "exec", "postgres", "psql", "-U", "devforge", "-d", "devforge_app",
         "-c", f"""
SELECT fact_index, fact_type, LEFT(evidence,80) AS evidence,
       faithful_score, faithful_method, nli_llm,
       LEFT(corrected_evidence,80) AS corrected
FROM review_facts
WHERE turn_id = '{tid}'::uuid AND source = 'extract_pipeline'
  AND fact_type IN ('user','thinking','text')
ORDER BY fact_index
LIMIT 20
         """],
        capture_output=True, text=True, timeout=10,
    )
    log(f"\n--- Turn {tid[:8]} ---")
    for line in db.stdout.strip().split("\n"):
        log(f"  {line}")

test_complete("Config C quality verification done")
