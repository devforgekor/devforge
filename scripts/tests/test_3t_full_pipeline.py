#!/usr/bin/env python3
"""3-turn full pipeline test: polish → extract → enrich → verify (long turns)."""
import os, sys, time

SCRIPTS_DIR = "/opt/projects/server/scripts"
os.chdir(SCRIPTS_DIR)
sys.path.insert(0, SCRIPTS_DIR)

from lib.test_common import test_setup, test_heartbeat, test_complete, log
from lib.db import psql_json
from lib.pod_manager import ensure_model

TOTAL = 3
LONG_TURN_IDS = [
    "a1faed6c-ffa1-4869-8dda-a468e8e5164f",
    "b7f65701-5689-4238-91fb-9caee1d3b8e8",
    "bb9c6363-cce3-4e60-aa05-8cd1f3e99d78",
]

PASS = 0
FAIL = 0


def ok(msg: str):
    global PASS
    PASS += 1
    log(f"  [PASS] {msg}")


def ng(tid: str, phase: str, detail: str = ""):
    global FAIL
    FAIL += 1
    d = f" — {detail}" if detail else ""
    log(f"  [FAIL] {phase} {tid[:12]}{d}")


def show_polish(turn_id):
    """Show polish results. Falls back to original clean text if not polished."""
    rows = psql_json(
        "SELECT"
        "  LEFT(user_turn_clean_polished,400) AS ut_pol,"
        "  LEFT(text_clean_polished,400) AS tx_pol,"
        "  LEFT(thinking_clean_polished,400) AS th_pol,"
        "  LEFT(user_turn_clean,400) AS ut,"
        "  LEFT(text_clean,400) AS tx,"
        "  LEFT(thinking_clean,400) AS th "
        f"FROM turns WHERE id = '{turn_id}'::uuid"
    )
    if not rows or not rows[0]:
        return {"ut_pol": "", "tx_pol": "", "th_pol": ""}
    r = rows[0]
    return {
        "ut_pol": (r.get("ut_pol") or ""),
        "tx_pol": (r.get("tx_pol") or ""),
        "th_pol": (r.get("th_pol") or ""),
        "ut": (r.get("ut") or ""),
        "tx": (r.get("tx") or ""),
        "th": (r.get("th") or ""),
    }


def show_enrich(turn_id):
    rows = psql_json(
        "SELECT evidence::text AS evidence "
        "FROM review_facts "
        f"WHERE turn_id = '{turn_id}'::uuid AND fact_type = 'enrich_meta' "
        "ORDER BY fact_index DESC LIMIT 1"
    )
    if rows and rows[0]:
        return str(rows[0].get("evidence", "N/A") or "N/A")
    return "N/A"


def show_verify(turn_id):
    rows = psql_json(
        "SELECT evidence::text AS evidence "
        "FROM review_facts "
        f"WHERE turn_id = '{turn_id}'::uuid AND fact_type = 'verify_result' "
        "ORDER BY fact_index DESC LIMIT 1"
    )
    if rows and rows[0]:
        return str(rows[0].get("evidence", "N/A") or "N/A")
    return "N/A"


def _run(cmd: str) -> int:
    return os.system(cmd + " 2>&1")


def main():
    t_start = time.monotonic()
    ctx = test_setup("test_long_3t", "3-turn full pipeline quality eval")

    # Step 0: Pick
    ids_list = ", ".join(f"'{i}'::uuid" for i in LONG_TURN_IDS[:TOTAL])
    turns = psql_json(
        "SELECT id, LENGTH(COALESCE(user_turn,'')) AS ut_len, "
        "  LENGTH(COALESCE(text,'')) AS tx_len, "
        "  LENGTH(COALESCE(thinking,'')) AS th_len, "
        "  user_turn_clean_polished IS NOT NULL AS polished "
        f"FROM turns WHERE id IN ({ids_list}) ORDER BY created_at ASC"
    )
    if not turns:
        ng("", "pick", "No turns found")
        test_complete()
        return
    turn_ids = [t["id"] for t in turns]
    log(f"=== Selected {len(turn_ids)} turns ===")
    for t in turns:
        c = (t.get("ut_len") or 0) + (t.get("tx_len") or 0) + (t.get("th_len") or 0)
        log(f"  {t['id'][:12]} — {c} chars, polished={t.get('polished', False)}")
    test_heartbeat("Turns selected")

    # ===== Phase 1: Polish =====
    log("=== Phase 1: Polish ===")
    ensure_model("polish")
    test_heartbeat("inference → polish")
    for tid in turn_ids:
        ret = _run(f"stdbuf -oL python3 pipelines/polish_batch.py --turn-id {tid} --no-llm")
        if ret == 0:
            ok(f"Polish (no-llm) {tid[:12]}")
        else:
            ng(tid[:12], "Polish")
    test_heartbeat("Polish phase done")

    # Show polish output
    for tid in turn_ids:
        p = show_polish(tid)
        log(f"\n  [polish {tid[:12]}]")
        if p.get("ut_pol"):
            log(f"    user_turn_clean_polished: {p['ut_pol'][:400]}")
        else:
            log(f"    user_turn_clean (original): {p.get('ut','')[:200]}")
        if p.get("tx_pol"):
            log(f"    text_clean_polished: {p['tx_pol'][:400]}")
        if p.get("th_pol"):
            log(f"    thinking_clean_polished: {p['th_pol'][:400]}")
    test_heartbeat("Polish quality shown")

    # ===== Phase 2: Extract =====
    log("=== Phase 2: Extract ===")
    ensure_model("extractor")
    test_heartbeat("inference → extractor")
    for tid in turn_ids:
        ret = _run(f"python3 pipelines/extract.py --turn-id {tid}")
        if ret == 0:
            ok(f"Extract {tid[:12]}")
        else:
            ng(tid[:12], "Extract")
    test_heartbeat("Extract phase done")

    # ===== Phase 3: Enrich =====
    log("=== Phase 3: Enrich ===")
    for tid in turn_ids:
        ret = _run(f"python3 pipelines/enrich.py --turn-id {tid}")
        if ret == 0:
            ok(f"Enrich {tid[:12]}")
        else:
            ng(tid[:12], "Enrich")
    test_heartbeat("Enrich phase done")

    # Show enrich output
    for tid in turn_ids:
        e = show_enrich(tid)
        log(f"\n  [enrich {tid[:12]}] {e[:400]}")
    test_heartbeat("Enrich quality shown")

    # ===== Phase 4: Verify =====
    log("=== Phase 4: Verify ===")
    for tid in turn_ids:
        ret = _run(f"python3 pipelines/day_verify.py --turn-id {tid}")
        if ret == 0:
            ok(f"Verify {tid[:12]}")
        else:
            ng(tid[:12], "Verify")
    test_heartbeat("Verify phase done")

    # Show verify output
    for tid in turn_ids:
        v = show_verify(tid)
        log(f"\n  [verify {tid[:12]}] {v[:400]}")
    test_heartbeat("Verify quality shown")

    elapsed = time.monotonic() - t_start
    log(f"\n{'='*60}")
    log(f"TEST COMPLETE: {elapsed:.0f}s | {PASS} pass / {FAIL} fail")
    log(f"{'='*60}")

    test_complete()


if __name__ == "__main__":
    main()
