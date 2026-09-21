#!/usr/bin/env python3
# Status: experimental
# Path: none — E2E test for _extract_edcr_freeform (EDC+R pipeline)
"""E2E test for _extract_edcr_freeform: OIE → Canonicalize → Refinement.

Phases:
  Phase 1 (OIE): Dual 4B (8082+8083) → FactArbiter → LLM conflict resolve
  Phase 2 (Canonicalize): SeqMatcher → Embed 3-tier (8081) → LLM-judge → dedup
  Phase 3 (Refinement): FACT-style context rewrite → Xplore(8083) re-extract → merge → cap 6

Usage:
  python3 scripts/tests/test_edcr_e2e.py
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# pipelines/ on sys.path for extract_llm's internal `from extract import _load_checkpoint`
_PIPELINES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pipelines")
if _PIPELINES not in sys.path:
    sys.path.insert(0, _PIPELINES)

from lib.test_common import log, test_complete, test_heartbeat, test_setup

N_TURNS = 3


def _query_turns(limit: int = 3) -> list[dict]:
    """Fetch smallest non-empty turns from DB by est_chars."""
    cmd = [
        "podman",
        "exec",
        "postgres",
        "psql",
        "-U",
        "devforge",
        "-d",
        "devforge_app",
        "-t",
        "-A",
        "-F",
        "||",
        "-c",
        f"SELECT id, COALESCE(user_turn, ''), COALESCE(text, '') "
        f"FROM turns "
        f"WHERE char_length(COALESCE(text,'')) > 50 "
        f"ORDER BY est_chars ASC NULLS LAST "
        f"LIMIT {limit}",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        log(f"  DB query failed: {r.stderr[:200]}")
        return []
    turns = []
    for line in r.stdout.strip().split("\n"):
        if not line or "||" not in line:
            continue
        parts = line.split("||", 2)
        if len(parts) == 3:
            tid, user_turn, text = parts
            turns.append({"id": tid.strip(), "user_turn": user_turn.strip(), "text": text.strip()})
    return turns


def _cleanup_checkpoints(turn_ids: list[str]) -> None:
    """Remove checkpoints and review_facts for test turns."""
    if not turn_ids:
        return
    ids = ", ".join(f"'{tid}'" for tid in turn_ids)
    subprocess.run(
        [
            "podman",
            "exec",
            "postgres",
            "psql",
            "-U",
            "devforge",
            "-d",
            "devforge_app",
            "-c",
            f"DELETE FROM review_facts WHERE turn_id IN ({ids}) AND source = 'extract_pipeline'",
        ],
        capture_output=True,
        timeout=10,
    )
    subprocess.run(
        [
            "podman",
            "exec",
            "postgres",
            "psql",
            "-U",
            "devforge",
            "-d",
            "devforge_app",
            "-c",
            f"DELETE FROM pipeline_checkpoints WHERE turn_id IN ({ids}) AND pipeline = 'extract'",
        ],
        capture_output=True,
        timeout=10,
    )


def _check_port(port: int, name: str) -> bool:
    """Quick health check for a single port."""
    import urllib.request

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            data=json.dumps(
                {
                    "model": "test",
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5):
            log(f"  {name} (:{port}): health OK")
            return True
    except Exception as e:
        log(f"  {name} (:{port}): {e}")
        return False


def run_e2e() -> dict:
    """Main E2E test: ensure extract mode, fetch turns, run pipeline, validate."""
    results = {
        "mode_ok": False,
        "phase1_ok": False,
        "phase2_ok": False,
        "cap_ok": False,
        "thread_safe": True,
        "errors": [],
        "warnings": [],
        "turns": {},
    }

    log("=" * 60)
    log("EDC+R E2E TEST")
    log("=" * 60)

    # ── 0. Ensure extractor (8082 day-extractor) ──
    log("\n--- Ensuring day-extractor (8082) ---")
    from lib.pod_manager import ensure_model

    try:
        ensure_model("day-extractor", skip_if_healthy=True)
        results["mode_ok"] = True
        log("  day-extractor ensured")
    except Exception as e:
        results["errors"].append(f"Failed to ensure day-extractor: {e}")
        return results

    time.sleep(5)

    # ── 1. Fetch turns ──
    turns = _query_turns(N_TURNS)
    if not turns:
        results["errors"].append("no turns found in DB")
        return results
    for t in turns:
        log(f"  turn {t['id'][:8]}: user={len(t['user_turn'])} text={len(t['text'])} chars")
    _cleanup_checkpoints([t["id"] for t in turns])

    # ── 2. Run pipeline ──
    log(f"\n--- Calling _extract_edcr_freeform ({len(turns)} turns) ---")
    test_heartbeat("EDC+R E2E: running pipeline")

    from pipelines.extract_llm import _extract_edcr_freeform

    t0 = time.monotonic()
    try:
        output = list(_extract_edcr_freeform(turns, dry_run=False))
        elapsed = time.monotonic() - t0
        log(f"\nPipeline completed in {elapsed:.0f}s")
    except Exception as e:
        import traceback

        results["errors"].append(f"Pipeline crashed: {e}\n{traceback.format_exc()}")
        return results

    # ── 3. Validate output ──
    log(f"\n--- Validation ({len(output)} turns) ---")

    for turn, ex_result, error in output:
        tid = turn["id"][:8]
        if error:
            results["warnings"].append(f"  [{tid}] turn error (non-fatal): {error}")
            continue
        if ex_result is None:
            results["warnings"].append(f"  [{tid}] ex_result is None")
            continue

        facts = ex_result.get("extractions", [])
        n_facts = len(facts)
        results["turns"][tid] = {"n_facts": n_facts, "facts": facts}
        log(f"  [{tid}] {n_facts} facts extracted")

        if n_facts == 0:
            log(f"  [{tid}] WARN: 0 facts")
            continue
        for fi, f in enumerate(facts):
            missing = [k for k in ("subject", "predicate", "object") if not f.get(k)]
            if missing:
                msg = f"  [{tid}] fact {fi} missing: {missing}"
                results["warnings"].append(msg)
                print(f"    WARN: {msg}")

    if results["warnings"]:
        log("\n  NON-FATAL WARNINGS:")
        for w in results["warnings"]:
            log(f"    {w}")

    # ── 4. Aggregate checks ──
    total_facts = sum(results["turns"][tid]["n_facts"] for tid in results["turns"])
    max_facts = max((results["turns"][tid]["n_facts"] for tid in results["turns"]), default=0)

    if total_facts > 0:
        results["phase1_ok"] = True
        log(f"  Phase 1 (OIE): PASS — {total_facts} total facts")

    if total_facts > 0:
        all_preds = []
        for tid in results["turns"]:
            for f in results["turns"][tid]["facts"]:
                all_preds.append(f.get("predicate", ""))
        snake_preds = sum(
            1 for p in all_preds if p and "_" in p and all(c.islower() or c == "_" for c in p)
        )
        if len(all_preds) > 0 and snake_preds == len(all_preds):
            results["phase2_ok"] = True
            log(f"  Phase 2 (Canonicalize): PASS — all {snake_preds} predicates canonicalized")
        elif snake_preds > 0:
            log(f"  Phase 2 (Canonicalize): PARTIAL — {snake_preds}/{len(all_preds)} canonicalized")
        else:
            log("  Phase 2 (Canonicalize): SKIP — no canonicalized predicates")

    if max_facts <= 6:
        results["cap_ok"] = True
        log(f"  Cap: PASS — max {max_facts}/6 facts per turn")
    else:
        results["errors"].append(f"  Cap VIOLATION: turn has {max_facts} facts (max 6)")

    seen_ids = set()
    for turn, _, _ in output:
        if turn["id"] in seen_ids:
            results["errors"].append(f"  DUPLICATE turn: {turn['id'][:8]}")
            results["thread_safe"] = False
        seen_ids.add(turn["id"])

    return results


def main():
    test_setup("edcr_e2e", "EDC+R E2E: OIE -> Canonicalize -> Refinement")

    results = run_e2e()

    log("\n" + "=" * 60)
    log("E2E RESULTS")
    log("=" * 60)
    for key in ["mode_ok", "phase1_ok", "phase2_ok", "cap_ok", "thread_safe"]:
        status = "PASS" if results.get(key) else "FAIL"
        log(f"  {key}: {status}")

    if results["warnings"]:
        log(f"\n  NON-FATAL WARNINGS ({len(results['warnings'])}):")
        for w in results["warnings"]:
            log(f"    {w}")

    if results["errors"]:
        log(f"\n  FATAL ERRORS ({len(results['errors'])}):")
        for e in results["errors"]:
            log(f"    {e}")

    test_complete("EDC+R E2E complete")

    if results["errors"]:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
