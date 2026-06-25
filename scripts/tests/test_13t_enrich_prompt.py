#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""13-turn full cycle test: polish → extract → enrich → day_verify.
   Cycle 1 (2 warm-up): no enrich_fewshot feedback
   Cycle 2 (11 main): batch enrich with feedback
"""
import json, os, sys, time
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.db import psql_ok, psql_json
from lib.pod_manager import ensure_model

TEST_TURNS = [
    "64c18c1a-995b-48ce-88df-f543dd48994c",
    "f6db0d1c-4813-4578-858d-beeb43ff8705",
    "37f7dbe1-6d30-4d3a-b812-bcb99d21c87c",
    "5541eaa4-cf57-4bd4-ab6f-6d76792fe189",
    "5c2dde84-914e-46ab-b5ca-e8814287b599",
    "c3f1df7e-60c8-4bab-a29b-579810005a94",
    "b743a040-515b-405f-ac9d-7ab03abc8bf8",
    "8d123e5b-1e3e-457b-bc37-de3edbc26299",
    "01f63985-1337-4395-85a7-9b2f1cf24c3e",
    "2859bed5-9b18-41a7-8b86-1ac2d1ba6fd5",
    "d452206c-7bf9-43fb-aa9c-71aab593e94c",
    "416bf41f-d416-407d-84c8-24adde4109ac",
    "16bd1c7c-081a-4d28-b57a-71106a162f4a",
]
WARMUP_COUNT = 2
FEWSHOT_PATH = os.path.join(SCRIPTS_DIR, "pipelines", "enrich_fewshot.json")
TOTAL = len(TEST_TURNS)

def main():
    t0 = time.monotonic()
    TEST = test_setup("13t_enrich_prompt", "13-turn enrich prompt + NLI verify test")

    # Step 0: Init enrich_fewshot
    log("[init] enrich_fewshot = []")
    os.makedirs(os.path.dirname(FEWSHOT_PATH), exist_ok=True)
    with open(FEWSHOT_PATH, "w") as f:
        json.dump([], f)

    # Step 1: Ensure Pod B
    log("[pod] Ensuring Pod B (enrich mode)")
    ensure_model("day_enrich")
    test_heartbeat("Pod B ready")

    # Step 2: Polish batch (all 13 turns)
    log(f"[polish] Running polish_batch on {TOTAL} turns")
    ret = os.system(f"python3 pipelines/polish_batch.py --limit {TOTAL}")
    assert ret == 0, f"polish_batch returned {ret}"
    test_heartbeat(f"Polish done: {TOTAL} turns")

    # Step 3: Extract (all 13 turns)
    log(f"[extract] Running extract on {TOTAL} turns")
    ret = os.system(f"python3 pipelines/extract.py --limit {TOTAL}")
    assert ret == 0, f"extract returned {ret}"
    test_heartbeat("Extract done")

    # Step 4: Enrich Cycle 1 — warm-up (2 turns, no feedback)
    warmup_ids = TEST_TURNS[:WARMUP_COUNT]
    log(f"[enrich] Cycle 1 — warm-up {WARMUP_COUNT} turns (no feedback)")
    for tid in warmup_ids:
        ret = os.system(f"python3 pipelines/enrich.py --turn-id {tid}")
        assert ret == 0, f"enrich warm-up {tid[:12]} returned {ret}"
    test_heartbeat(f"Enrich warm-up done: {WARMUP_COUNT} turns")

    # Step 5: Enrich Cycle 2 — main 11 turns with feedback
    main_ids = TEST_TURNS[WARMUP_COUNT:]
    log(f"[enrich] Cycle 2 — main {len(main_ids)} turns")
    # Simulate feedback: mark warm-up as good/bad in enrich_fewshot
    # The watchdog normally does this, but we run manually
    # Run enrich with --limit to batch-process remaining turns
    ret = os.system(f"python3 pipelines/enrich.py --limit {len(main_ids)}")
    # If --limit processed remaining turns, great. Otherwise run per-turn.
    if ret != 0:
        log("[enrich] --limit processed nothing, running per-turn")
        for tid in main_ids:
            ret = os.system(f"python3 pipelines/enrich.py --turn-id {tid}")
    test_heartbeat("Enrich main done")

    # Step 6: Day verify (all 13 turns)
    log(f"[verify] Running day_verify on all turns")
    ret = os.system(f"python3 pipelines/day_verify.py --limit {TOTAL}")
    test_heartbeat("Verify done")

    elapsed = time.monotonic() - t0
    log(f"\n{'='*60}")
    log(f"TEST COMPLETE: {elapsed:.0f}s elapsed")
    log(f"{'='*60}")

    test_complete()

if __name__ == "__main__":
    main()
