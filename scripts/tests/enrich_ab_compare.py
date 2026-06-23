#!/usr/bin/env python3
# Status: experimental
# Path: manual — enrich A/B quality comparison (day-enrich 9B Q4 vs day-verifier 7B Q8)
"""Enrich A/B Quality Comparison — runs same turns through two models and compares.

Usage:
  python3 scripts/tests/enrich_ab_compare.py                    # full A/B flow
  python3 scripts/tests/enrich_ab_compare.py --phase A_only     # only A
  python3 scripts/tests/enrich_ab_compare.py --phase B_only     # only B (after A)
"""

import json, os, sys, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.db import psql_ok, esc_sql, psql, psql_json
from lib.pod_manager import ensure_model, _check_model_identity
from lib.test_common import test_setup, test_complete, test_heartbeat

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "eval")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

# 10 turns from v3 test (had review_facts + wrong-model enrich)
TURN_IDS = [
    "cb0e241f-4397-4dda-8af5-5ba3b6ae534e",
    "1b326503-4950-4dff-a9d9-02148450d818",
    "f40b05ab-a3b9-42e7-a975-fb94722aedb1",
    "988336db-d047-4dab-8252-638021c73318",
    "958a2c2f-4fb6-44e3-9ad5-f196f5e9b6da",
    "980601c8-9c81-41fd-95ca-d5f4283a005a",
    "8f66db13-53ce-4535-be40-44a0ae49cc67",
    "cfe99924-791b-46b8-844f-ce3a4b3b5a20",
    "31f71819-8f85-44fe-8a09-50534bf76060",
    "c2893937-7eda-4172-91f7-e73f51ec6666",
]


def delete_enrich_meta(turn_ids):
    ids = ", ".join(f"'{esc_sql(t)}'::uuid" for t in turn_ids)
    sql = f"DELETE FROM review_facts WHERE turn_id IN ({ids}) AND fact_type = 'enrich_meta'"
    psql_ok(sql)
    n = psql(f"SELECT COUNT(*) FROM review_facts WHERE turn_id IN ({ids}) AND fact_type = 'enrich_meta'",
             timeout=5)
    if n.strip() == "0":
        print(f"  enrich_meta cleared ({len(turn_ids)} turns)")
    else:
        print(f"  WARNING: {n.strip()} enrich_meta rows remain")


def read_enrich_results(turn_ids):
    """Read enrich_meta results from review_facts for given turns."""
    ids = ", ".join(f"'{esc_sql(t)}'::uuid" for t in turn_ids)
    rows = psql_json(
        f"SELECT turn_id::text, fact_index, evidence, extract_model, created_at::text "
        f"FROM review_facts "
        f"WHERE turn_id IN ({ids}) AND fact_type = 'enrich_meta' "
        f"ORDER BY turn_id, fact_index",
        timeout=10,
    )
    results = {}
    for r in rows:
        tid = r["turn_id"]
        if tid not in results:
            results[tid] = []
        evidence = r.get("evidence", "{}")
        try:
            data = json.loads(evidence)
        except (json.JSONDecodeError, TypeError):
            data = {"raw": evidence[:200]}
        data["_fact_index"] = r["fact_index"]
        data["_extract_model"] = r["extract_model"]
        data["_db_created"] = r["created_at"]
        results[tid].append(data)
    return results


def run_phase(phase_label, model_key, call_model):
    """Load model, delete old enrich_meta, run enrich, read results."""
    print(f"\n{'=' * 60}")
    print(f"Phase {phase_label}: {model_key}")
    print(f"{'=' * 60}")

    # Step 1: Load model
    print(f"\n  Loading {model_key} on :8082...")
    ok = ensure_model(model_key)
    if not ok:
        print(f"  FATAL: ensure_model({model_key}) failed")
        return None
    # Verify model identity (race w/ watchdog recovery can overwrite env)
    if not _check_model_identity(8082, model_key):
        print(f"  FATAL: :8082 wrong model after ensure_model — retrying once")
        ok = ensure_model(model_key)
        if not ok or not _check_model_identity(8082, model_key):
            print(f"  FATAL: :8082 still wrong model after retry — aborting")
            return None
    time.sleep(5)

    # Step 2: Clear old enrich_meta
    delete_enrich_meta(TURN_IDS)

    # Step 3: Import and run enrich pipeline per-turn (bypasses main())
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipelines"))
    from pipelines.enrich import enrich_pipeline

    t0 = time.monotonic()
    processed = 0
    failed = 0
    summary = {"processed": 0, "failed": 0}
    for tid in TURN_IDS:
        result = enrich_pipeline(turn_id=tid, model=call_model, dry_run=False)
        p = result.get("processed", 0)
        f = result.get("failed", 0)
        processed += p
        failed += f
        print(f"  {tid[:12]} — processed={p} failed={f}", flush=True)
    elapsed = round(time.monotonic() - t0, 1)
    summary = {"processed": processed, "failed": failed, "elapsed_s": elapsed}
    print(f"  enrich_pipeline done in {elapsed}s: {summary}")

    # Step 4: Read results from DB
    results = read_enrich_results(TURN_IDS)
    done = sum(1 for r in results.values() if r)
    print(f"  DB results: {done}/{len(TURN_IDS)} turns with enrich_meta")

    return results


def save_snapshot(phase, results):
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SNAPSHOT_DIR, f"enrich_ab_{phase}_{timestamp}.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"  Snapshot: {path}")
    return path


def compare(A, B):
    print(f"\n{'=' * 60}")
    print("COMPARISON: A (9B Q4) vs B (7B Q8)")
    print(f"{'=' * 60}")

    headers = ["Turn", "Ent. f/fn", "", "TLDR len", "", "Tags", "", "Model"]
    print(f"{'  '.join(f'{h:<10}' for h in headers)}")
    print("-" * 100)

    a_fail = b_fail = 0
    a_fn = b_fn = 0
    a_f = b_f = 0
    a_tlen = b_tlen = 0
    a_tags = b_tags = 0
    a_count = b_count = 0

    for tid in TURN_IDS:
        a_meta = A.get(tid, [{}])[0] if A and A.get(tid) else {}
        b_meta = B.get(tid, [{}])[0] if B and B.get(tid) else {}
        label = tid[:12]

        if a_meta.get("_extract_model"):
            a_count += 1
        if b_meta.get("_extract_model"):
            b_count += 1

        a_ents = a_meta.get("entities", {}) if a_meta.get("_extract_model") else {}
        b_ents = b_meta.get("entities", {}) if b_meta.get("_extract_model") else {}

        a_ffn = f"{len(a_ents.get('files',[]))}/{len(a_ents.get('functions',[]))}" if a_ents else "-"
        b_ffn = f"{len(b_ents.get('files',[]))}/{len(b_ents.get('functions',[]))}" if b_ents else "-"
        a_t = len(a_meta.get("tldr", "")) if a_meta.get("_extract_model") else 0
        b_t = len(b_meta.get("tldr", "")) if b_meta.get("_extract_model") else 0
        a_tg = len(a_meta.get("tags", [])) if a_meta.get("_extract_model") else 0
        b_tg = len(b_meta.get("tags", [])) if b_meta.get("_extract_model") else 0
        a_m = a_meta.get("_extract_model", "?")
        b_m = b_meta.get("_extract_model", "?")

        print(f"{label:<12} {a_ffn:<14} {b_ffn:<14} {a_t:<10} {b_t:<10} {a_tg:<8} {b_tg:<8} {a_m:<12} {b_m:<12}")

        if a_ents:
            a_fn += len(a_ents.get("functions", []))
            a_f += len(a_ents.get("files", []))
            a_tlen += a_t
            a_tags += a_tg
        else:
            a_fail += 1
        if b_ents:
            b_fn += len(b_ents.get("functions", []))
            b_f += len(b_ents.get("files", []))
            b_tlen += b_t
            b_tags += b_tg
        else:
            b_fail += 1

    print("-" * 100)
    print(f"\nSUMMARY:")
    print(f"  A (9B Q4):     {a_count}/{len(TURN_IDS)} enriched | {a_f}f {a_fn}fn | tldr_avg={a_tlen/max(a_count,1):.0f}ch | tags_avg={a_tags/max(a_count,1):.1f}")
    print(f"  B (7B Q8):     {b_count}/{len(TURN_IDS)} enriched | {b_f}f {b_fn}fn | tldr_avg={b_tlen/max(b_count,1):.0f}ch | tags_avg={b_tags/max(b_count,1):.1f}")


def main():
    phase = "full"
    for i, a in enumerate(sys.argv):
        if a == "--phase" and i + 1 < len(sys.argv):
            phase = sys.argv[i + 1]

    print(f"Enrich A/B Quality Comparison")
    print(f"  Turns: {len(TURN_IDS)}")
    print(f"  Phase: {phase}")

    test_setup("enrich_ab", "Enrich A/B quality comparison (9B Q4 vs 7B Q8)")

    results = {"A": None, "B": None}

    if phase in ("full", "A_only"):
        results["A"] = run_phase("A", "day-enrich", "day_enrich")
        if results["A"]:
            save_snapshot("A", results["A"])
        test_heartbeat("Phase A done")

    if phase in ("full", "B_only"):
        results["B"] = run_phase("B", "day-verifier", "day_verify")
        if results["B"]:
            save_snapshot("B", results["B"])
        test_heartbeat("Phase B done")

    if results["A"] and results["B"]:
        compare(results["A"], results["B"])

    test_complete()

    print("\nDone.")


if __name__ == "__main__":
    main()
