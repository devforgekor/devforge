#!/usr/bin/env python3
"""test_predicate_extract.py — Phase B: free-form predicate + atomic claim extraction test.

Calls extract.py --turn-id for each selected turn, validates free-form predicate
format, atomic claim splitting, and triple completeness.
"""

import json
import os
import re
import sys
import time

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.common import log
from lib.db import psql_json
from lib.test_common import test_complete, test_heartbeat, test_setup
from pipelines.extract_llm import _calc_max_tokens, _calc_timeout

_RESULTS_FILE = "/tmp/predicate_test_results.json"
_OLD_PREDICATES = {
    "config_set",
    "function_added",
    "function_modified",
    "bug_observed",
    "decision_made",
    "explanation_provided",
}

CRITERIA = {
    "non_empty_turn_ratio": 0.80,  # >= 80% turns produce >= 1 fact
    "predicate_diversity": 0.50,  # >= 50% predicates NOT from old 5-value set
    "predicate_format_valid": 0.90,  # >= 90% of predicates match snake_case
    "avg_facts_per_turn": 2.0,  # Atomic splitting increases fact count
    "triple_completeness": 0.70,  # >= 70% have all 3 (s/p/o)
}


def set_status(state, detail=""):
    status = {"state": state, "detail": detail, "ts": time.time()}
    with open(_RESULTS_FILE, "w") as f:
        json.dump(status, f, indent=2)
    log(f"[status] {state}: {detail}")


def select_turns():
    """Select 10 diverse technical turns pending extraction (broad discussion)."""
    q = """
        WITH candidates AS (
            SELECT id::text, text, COALESCE(user_turn,'') AS user_turn,
                   LENGTH(COALESCE(user_turn,'') || ' ' || COALESCE(text,'')) AS total_len,
                   CASE WHEN LOWER(COALESCE(NULLIF(user_turn,''),text))
                     ~ '(compare|recommend|suggest|plan|propose|think|consider|'
                     'versus|vs|better|faster|instead|alternative|switch|migrat|'
                     'port|config|model|pod|pipeline|extract|error|bug|change|'
                     'update|deploy|restart|install|timer|service|systemd|'
                     'db|sql|memory|cpu|swap|disk|container|network|schema|'
                     'watchdog|recover|question|ask|why|how|whatif)'
                     THEN 20 ELSE 0 END AS tech_score
            FROM turns
            WHERE pipeline_state IN ('pending','scanned')
              AND LENGTH(text) > 200
        )
        SELECT id, text, user_turn, total_len FROM candidates
        WHERE tech_score > 0
        ORDER BY total_len ASC
    """
    rows = psql_json(q)
    if not rows:
        log("Falling back to all pending turns")
        q = """SELECT id::text, text, COALESCE(user_turn,'') AS user_turn,
                      LENGTH(text) AS total_len
               FROM turns WHERE pipeline_state IN ('pending','scanned') AND LENGTH(text) > 200
               ORDER BY total_len ASC"""
        rows = psql_json(q)

    n = len(rows)
    if n <= 10:
        return rows
    third = n // 3
    import random

    random.seed(42)
    selected = (
        random.sample(rows[:third], min(3, len(rows[:third])))
        + random.sample(rows[third : 2 * third], min(3, len(rows[third : 2 * third])))
        + random.sample(rows[2 * third :], min(4, len(rows[2 * third :])))
    )[:10]

    log(f"Selected {len(selected)} turns")
    for s in selected:
        log(f"  [{s['id'][:8]}] ({s['total_len']}c) {s['text'][:80].replace(chr(10), ' ')}")
    return selected


def run_turn_extractions(turns):
    """Run extract.py --turn-id for each turn with per-section summed timeout."""
    import subprocess

    completed = []
    env = os.environ.copy()
    env["PYTHONPATH"] = _SCRIPTS_DIR

    for i, turn in enumerate(turns):
        tid = turn["id"]
        total_chars = turn["total_len"]

        # Per-section timeout: user section → 6s sleep → text section
        def _section_timeout(text: str) -> int:
            if not text:
                return 0
            max_tok = _calc_max_tokens(len(text))
            if max_tok is None:
                return 0  # overflow → fast skip
            return _calc_timeout(len(text), max_tok)

        user_sec = _section_timeout(turn.get("user_turn", "") or "")
        text_sec = _section_timeout(turn.get("text", "") or "")
        timeout = user_sec + 6 + text_sec
        timeout = min(timeout, 3600)  # absolute ceiling

        test_heartbeat(f"Extracting turn {i + 1}/{len(turns)}: {tid[:8]}")
        set_status("extracting", f"Turn {i + 1}: {tid[:8]} ({total_chars}c, timeout={timeout}s)")
        log(
            f"  [{tid[:8]}] timeout={timeout}s (user={user_sec}s+6+text={text_sec}s) total_len={total_chars}c"
        )

        t0 = time.time()
        try:
            result = subprocess.run(
                [sys.executable, "pipelines/extract.py", "--turn-id", tid],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
                cwd=_SCRIPTS_DIR,
            )
        except subprocess.TimeoutExpired:
            log(f"  Turn {i + 1}: TIMEOUT after {timeout}s")
            completed.append(False)
            continue
        elapsed = time.time() - t0
        ok = result.returncode == 0
        last_lines = (result.stdout or "")[-500:] + (result.stderr or "")[-500:]
        log(
            f"  Turn {i + 1}: exit={result.returncode}, elapsed={elapsed:.0f}s, out={len(result.stdout or '')}b"
        )
        if not ok and last_lines.strip():
            log(f"  Error: {last_lines[:300]}")
        completed.append(ok)

    return all(completed)


_SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


def check_results(turn_ids):
    ids_str = ", ".join(f"'{eid}'" for eid in turn_ids)

    total = psql_json(f"SELECT COUNT(*) AS cnt FROM review_facts WHERE turn_id IN ({ids_str})")
    total_cnt = total[0]["cnt"] if total else 0

    preds = psql_json(f"""
        SELECT predicate, COUNT(*) AS cnt FROM review_facts
        WHERE turn_id IN ({ids_str}) AND predicate IS NOT NULL AND predicate != ''
        GROUP BY predicate ORDER BY cnt DESC""")

    valid = psql_json(f"""
        SELECT COUNT(*) AS cnt FROM review_facts
        WHERE turn_id IN ({ids_str})
          AND subject IS NOT NULL AND subject != ''
          AND predicate IS NOT NULL AND predicate != ''
          AND object IS NOT NULL AND object != ''""")
    valid_cnt = valid[0]["cnt"] if valid else 0

    zero = psql_json(f"""
        SELECT t.id::text, LEFT(t.text, 100) AS preview FROM turns t
        WHERE t.id IN ({ids_str})
          AND NOT EXISTS (SELECT 1 FROM review_facts rf WHERE rf.turn_id = t.id)""")

    # --- Criteria evaluations ---

    # non_empty_turn_ratio
    non_empty = len(turn_ids) - len(zero)
    non_empty_ratio = round(non_empty / max(len(turn_ids), 1), 3)

    # predicate_diversity: predicates NOT from old set
    pred_map = {r["predicate"]: r["cnt"] for r in preds} if preds else {}
    new_preds = {k: v for k, v in pred_map.items() if k not in _OLD_PREDICATES}
    total_pred_count = sum(pred_map.values())
    diversity_ratio = round(sum(new_preds.values()) / max(total_pred_count, 1), 3)

    # predicate_format_valid: snake_case format check
    if preds:
        valid_format = sum(
            c for r in preds if _SNAKE_CASE_RE.match(r["predicate"]) for c in [r["cnt"]]
        )
        format_ratio = round(valid_format / max(total_pred_count, 1), 3)
    else:
        valid_format = 0
        format_ratio = 0.0

    # avg_facts_per_turn
    avg_facts = round(total_cnt / max(len(turn_ids), 1), 2)

    # triple_completeness
    completeness = round(valid_cnt / max(total_cnt, 1), 3)

    samples = psql_json(f"""
        SELECT turn_id::text, subject, predicate, object, qualifiers,
               LEFT(evidence, 200) AS evidence_short
        FROM review_facts WHERE turn_id IN ({ids_str})
          AND subject IS NOT NULL LIMIT 15""")

    criteria_results = {
        "non_empty_turn_ratio": {
            "value": non_empty_ratio,
            "min": CRITERIA["non_empty_turn_ratio"],
            "pass": non_empty_ratio >= CRITERIA["non_empty_turn_ratio"],
        },
        "predicate_diversity": {
            "value": diversity_ratio,
            "min": CRITERIA["predicate_diversity"],
            "pass": diversity_ratio >= CRITERIA["predicate_diversity"],
        },
        "predicate_format_valid": {
            "value": format_ratio,
            "min": CRITERIA["predicate_format_valid"],
            "pass": format_ratio >= CRITERIA["predicate_format_valid"],
        },
        "avg_facts_per_turn": {
            "value": avg_facts,
            "min": CRITERIA["avg_facts_per_turn"],
            "pass": avg_facts >= CRITERIA["avg_facts_per_turn"],
        },
        "triple_completeness": {
            "value": completeness,
            "min": CRITERIA["triple_completeness"],
            "pass": completeness >= CRITERIA["triple_completeness"],
        },
    }
    all_pass = all(c["pass"] for c in criteria_results.values())

    return {
        "total_facts": total_cnt,
        "valid_facts": valid_cnt,
        "valid_ratio": completeness,
        "predicates": pred_map,
        "new_predicates": new_preds,
        "old_predicate_set": sorted(_OLD_PREDICATES),
        "samples": samples or [],
        "zero_fact_turns": zero or [],
        "turns_tested": len(turn_ids),
        "criteria": criteria_results,
        "criteria_all_pass": all_pass,
    }


def main():
    set_status("starting", "Initializing")
    ctx = test_setup("predicate_extract_b", "Predicate extraction Phase B: free-form + atomic")
    log(f"Test: {json.dumps(ctx)}")

    # Select turns
    set_status("selecting_turns", "Selecting 10 diverse technical turns")
    turns = select_turns()
    if not turns:
        set_status("error", "No turns found")
        test_complete("error: no turns")
        sys.exit(1)

    turn_ids = [t["id"] for t in turns]
    test_heartbeat(f"Selected {len(turn_ids)} turns")

    # Run extraction
    set_status("extracting", f"Processing {len(turn_ids)} turns (free-form predicate)")
    success = run_turn_extractions(turns)

    if not success:
        set_status("error", "Some extractions failed")
        test_complete("error: partial extract failure")
        sys.exit(1)

    # Check structured output
    set_status("checking", "Checking structured output")
    results = check_results(turn_ids)
    results["turns"] = [
        {"id": t["id"], "len": t["total_len"], "preview": t["text"][:80].replace(chr(10), " ")}
        for t in turns
    ]
    results["state"] = "completed"
    passed = results["criteria_all_pass"]
    results["detail"] = (
        f"{'PASS' if passed else 'FAIL'}: "
        f"{results['valid_facts']}/{results['total_facts']} valid, "
        f"{len(results['predicates'])} pred types, "
        f"diversity={results['criteria']['predicate_diversity']['value']}"
    )
    set_status("completed", results["detail"])

    with open(_RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2, default=str)

    # Report
    log(f"\n{'=' * 60}")
    log(
        f"RESULTS: {results['valid_facts']}/{results['total_facts']} facts, "
        f"{results['turns_tested']} turns"
    )
    log(f"Predicates: {json.dumps(results['predicates'])}")
    log(f"New predicates (not from old set): {json.dumps(results['new_predicates'])}")
    log("\nCriteria:")
    for name, cr in results["criteria"].items():
        status = "✅" if cr["pass"] else "❌"
        log(f"  {status} {name}: {cr['value']} (min={cr['min']})")
    if results.get("zero_fact_turns"):
        log(f"\nZero-fact turns: {len(results['zero_fact_turns'])}")
        for z in results["zero_fact_turns"]:
            log(f"  [{z['id'][:8]}] {z['preview']}")
    log(f"\n{'=' * 60}")
    log(f"Overall: {'PASS ✅' if passed else 'FAIL ❌'}")
    log(f"{'=' * 60}\n")

    test_complete(f"done: {results['valid_facts']} facts, {'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
