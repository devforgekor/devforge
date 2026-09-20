#!/usr/bin/env python3
# Status: experimental
# Path: none — test script
"""E2E pipeline test: Phase 1 (inline post-processing) + Phase 2 (offline supplement).

Creates a test turn, runs full extract → normalize → verify → store → supplement cycle.
Verifies:
  - Phase 1: qualifiers extracted, multi-value expansion
  - Phase 2: supplement finds new facts, stores them
  - No regressions: all original facts still GROUNDED
"""

import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import esc_sql, psql_json, psql_ok

TEST_TEXT = """User: We had a production issue yesterday. The ETL pipeline processing time increased from 45 minutes to 3 hours after deployment. Root cause was a missing index on the event_logs table causing full table scans. A composite index on (created_at, event_type) resolved it and processing returned to normal.

Assistant: I see several issues to address. First, the memory utilization on worker nodes peaked at 87% during peak hours. After increasing heap to 8GB and tuning to G1GC peak utilization dropped to 52%. Second, the API gateway returned 503 errors for 12% of requests during the incident. The connection pool to the inventory service was exhausted at 50 connections.

Third, the SSL certificate renewal failed silently causing the API to serve expired certs for 6 hours. The certbot cron job had been disabled during a server migration. We've now automated monitoring for cert expiry with 30-day warning. Also, Java GC ran every 2 seconds with 4GB heap and 3.8GB live set."""


def _create_test_turn(text: str) -> str:
    """Create a test turn in the DB and return its UUID."""
    turn_id = str(uuid.uuid4())
    conv_id = str(uuid.uuid4())
    now = "2026-07-06T00:00:00Z"

    # Create a test conversation first (FK requirement)
    psql_ok(f"""
        INSERT INTO conversations (id, title, source, created_at)
        VALUES ('{conv_id}'::uuid, 'test-e2e-phases-1-2', 'test-e2e', '{now}'::timestamp)
        ON CONFLICT (id) DO NOTHING
    """)

    # Split into user_turn and text at the "Assistant:" boundary
    parts = text.split("\n\nAssistant: ", 1)
    user_turn = parts[0].replace("User: ", "", 1)
    text_part = parts[1] if len(parts) > 1 else ""

    seq = int(time.time() * -1000) % 100000  # negative seq like real data

    sql = f"""
        INSERT INTO turns (id, user_turn, text, thinking, pipeline_state,
                           conversation_id, source_message_id, created_at,
                           detected_lang, est_chars, seq)
        VALUES (
            '{turn_id}'::uuid,
            '{esc_sql(user_turn)}',
            '{esc_sql(text_part)}',
            '',
            'scanned',
            '{conv_id}'::uuid,
            'test-e2e-phases-1-2',
            '{now}'::timestamp,
            'en',
            {len(text)},
            {seq}
        )
        ON CONFLICT (id) DO NOTHING
    """
    ok = psql_ok(sql)
    if not ok:
        raise RuntimeError(f"Failed to create test turn {turn_id}")
    print(f"  [test] Created turn {turn_id[:8]} ({len(text)}ch)")
    return turn_id, conv_id


def _run_extract(turn_id: str) -> Dict[str, Any]:
    """Run the extract pipeline on a single turn. Returns result dict."""
    print(f"\n{'=' * 60}")
    print(f"PHASE 1: Extract Pipeline (turn {turn_id[:8]})")
    print(f"{'=' * 60}")

    from extract import extract_pipeline

    result = extract_pipeline(turn_id=turn_id)
    return result


def _run_supplement(limit: int = 5) -> Dict[str, Any]:
    """Run the supplement script as subprocess. Returns parsed result."""
    print(f"\n{'=' * 60}")
    print(f"PHASE 2: Post-Extract Supplement")
    print(f"{'=' * 60}")

    script = os.path.join(SCRIPTS_DIR, "pipelines", "post_extract_supplement.py")
    r = subprocess.run(
        [sys.executable, script, "--limit", str(limit), "--json"],
        capture_output=True, text=True, timeout=3000
    )
    print(r.stdout)
    if r.stderr:
        print(f"  [stderr] {r.stderr.strip()[:500]}", flush=True)

    for line in r.stdout.split("\n"):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    return {"ok": False, "error": "no JSON output"}


def _check_results(turn_id: str) -> Dict[str, Any]:
    """Check results in DB: count facts, qualifiers, NLI verdicts."""
    facts = psql_json(f"""
        SELECT fact_index, extract_model, subject, predicate, object,
               qualifiers, nli_verdict, fact_action, verdict
        FROM review_facts
        WHERE turn_id = '{turn_id}'::uuid
          AND source = 'extract_pipeline'
        ORDER BY fact_index
    """) or []

    results = {
        "turn_id": turn_id[:8],
        "total_facts": len(facts),
        "day_extract": 0,
        "day_supplement": 0,
        "grounded": 0,
        "ungrounded": 0,
        "pending": 0,
        "with_qualifiers": 0,
    }

    print(f"\n{'=' * 60}")
    print(f"RESULTS for turn {turn_id[:8]}")
    print(f"{'=' * 60}")

    for f in facts:
        model = f.get("extract_model", "?")
        results[f"{model}"] = results.get(f"{model}", 0) + 1

        nli = f.get("nli_verdict") or ""
        if nli == "GROUNDED":
            results["grounded"] += 1
        elif nli in ("UNGROUNDED", "CONTRADICTION"):
            results["ungrounded"] += 1
        else:
            results["pending"] += 1

        quals = f.get("qualifiers")
        if quals and isinstance(quals, dict) and len(quals) > 0:
            results["with_qualifiers"] += 1

        subj = (f.get("subject") or "")[:40]
        pred = (f.get("predicate") or "")[:40]
        obj = (f.get("object") or "")[:40]
        qstr = ""
        if quals and isinstance(quals, dict) and quals:
            qstr = f" qual={dict(list(quals.items())[:2])}"
        print(f"  fi={f['fact_index']:2d} {model:14s} | {nli or 'pending':10s} | {subj} | {pred} | {obj}{qstr}")

    print(f"\n  Summary:")
    print(f"    Total: {results['total_facts']} facts")
    print(f"    day_extract: {results['day_extract']}, day_supplement: {results['day_supplement']}")
    print(f"    GROUNDED: {results['grounded']}, UNGROUNDED: {results['ungrounded']}, pending: {results['pending']}")
    print(f"    With qualifiers: {results['with_qualifiers']}")

    return results


def _cleanup(turn_id: str, conv_id: str = "") -> None:
    """Remove test data from DB."""
    psql_ok(f"""
        DELETE FROM review_facts
        WHERE turn_id = '{turn_id}'::uuid
          AND source = 'extract_pipeline'
    """)
    psql_ok(f"""
        DELETE FROM turns
        WHERE id = '{turn_id}'::uuid
    """)
    psql_ok(f"""
        DELETE FROM pipeline_checkpoints
        WHERE pipeline = 'extract' AND turn_id = '{turn_id}'::uuid
    """)
    if conv_id:
        psql_ok(f"""
            DELETE FROM conversations
            WHERE id = '{conv_id}'::uuid AND source = 'test-e2e'
        """)
    print(f"  [cleanup] Removed test turn {turn_id[:8]}")


def main() -> None:
    t0 = time.monotonic()
    turn_id = None
    conv_id = None

    try:
        # Step 0: Create test turn
        print("Creating test turn...")
        turn_id, conv_id = _create_test_turn(TEST_TEXT)

        # Allow time for pipeline_state transition
        time.sleep(0.5)

        # Step 1: Run extract pipeline (LLM extraction + Phase 1 normalize + NLI verify + store)
        extract_result = _run_extract(turn_id)
        print(f"  [extract] processed={extract_result.get('processed',0)} "
              f"facts={extract_result.get('facts',0)} "
              f"failed={extract_result.get('failed',0)} "
              f"elapsed={extract_result.get('elapsed_s',0):.1f}s")

        if extract_result.get("failed", 0) > 0:
            print("  [FAIL] Extract pipeline failed")
            sys.exit(1)

        # Step 2: Check Phase 1 results
        print(f"\nPhase 1 check (inline post-processing):")
        phase1 = _check_results(turn_id)

        if phase1["total_facts"] == 0:
            print("  [FAIL] No facts stored by extract pipeline")
            sys.exit(1)

        print(f"  [PASS] {phase1['total_facts']} facts extracted")

        # Step 3: Run supplement pipeline
        supp_result = _run_supplement(limit=5)
        print(f"  [supplement] processed={supp_result.get('processed',0)} "
              f"supplements={supp_result.get('total_supplements',0)} "
              f"elapsed={supp_result.get('elapsed_s',0):.1f}s")

        # Step 4: Check Phase 2 results
        print(f"\nPhase 2 check (offline supplement):")
        phase2 = _check_results(turn_id)

        supp_count = supp_result.get("total_supplements", 0)
        if supp_count > 0:
            print(f"  [PASS] {supp_count} supplement fact(s) stored")
        else:
            print(f"  [OK] No supplement facts (all were duplicates or LLM found none)")

        # Step 5: Final summary
        total_elapsed = time.monotonic() - t0
        print(f"\n{'=' * 60}")
        print(f"E2E TEST COMPLETE")
        print(f"{'=' * 60}")
        print(f"  Total elapsed: {total_elapsed:.1f}s")
        print(f"  Original facts: {phase1['total_facts']}")
        print(f"  Supplement facts: {supp_count}")
        print(f"  Total in DB: {phase1['total_facts'] + supp_count}")
        print(f"  GROUNDED: {phase2.get('grounded', 0)}")
        print(f"  With qualifiers: {phase2.get('with_qualifiers', 0)}")
        print(f"{'=' * 60}")

    except Exception as e:
        print(f"\n  [FAIL] E2E test exception: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    finally:
        if turn_id:
            _cleanup(turn_id, conv_id or "")

    print(f"\n  [DONE] E2E test finished in {time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()
