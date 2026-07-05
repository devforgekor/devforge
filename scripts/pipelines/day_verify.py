#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Phase 4 (verify after extract, before enrich)
"""Day Verify Pipeline — predicate NLI with Veritas-8B Fact Checker.

Verifies extracted predicates against source text using Veritas-8B,
fine-tuned for factual consistency NLI.

Pipeline position: AFTER extract → BEFORE enrich
State flow: extracted → verified

Method:
  Pass 1 — Binary YES/NO (batch all predicates per turn):
    YES  → GROUNDED
    NO   → Pass 2
  Pass 2 — CONTRADICTION check (per predicate):
    YES  → CONTRADICTION
    NO   → UNGROUNDED

Veritas is a Qwen3-8B finetune specialized for fact-checking (MiniCheck
bespoke format, 75.47% LLM-AggreFact balanced accuracy). Two-pass binary
NLI stays close to its training distribution while supporting 4-way verdicts.

Caveats:
  - Source chunked into ~800c segments; each chunk must fit with claims in Veritas ctx=4096
  - CONTRADICTION requires an extra LLM call per rejected claim
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import PSQL, esc_sql, psql_json, psql_ok
from lib.infra.preflight import preflight_checks
from lib.llm_client import call_llm_with_retry
from lib.model_registry import MODEL_METADATA
from lib.pod_manager import ensure_dual, ensure_model, wait_health
from lib.watchdog.messenger import heartbeat

BATCH_LIMIT = 50
PARALLEL = 2
SOLO_THRESHOLD = 5000

# Veritas timeout: same formula pattern as extract_llm._calc_timeout
_VERIFY_TIMEOUT_BASE = 10
_VERIFY_TIMEOUT_PER_CHAR = 0.02  # ~50 chars/s prefill on 2-thread ARM
_VERIFY_TIMEOUT_PER_TOK = 5  # decode buffer per gen token
_VERIFY_GEN_TIME_BUF = 20  # spike/GC/swap buffer
_VERIFY_TIMEOUT_CAP = 120  # hard cap

"""Veritas Bis (Bespoke) operating mode.

Document: {source}
Claim: {claim}

→ "Yes" (supported, GROUNDED) or "No" (unsupported, → CONTRADICTION check)
No system prompt, no instructions — Veritas is fine-tuned on Document/Claim pairs.
See https://github.com/Liyan06/MiniCheck and ollama bespoke-minicheck.
"""

_VERITAS_PROMPT_BINARY = "Document: {source}\nClaim: {claim}"

_VERITAS_PROMPT_CONTRADICT = (
    "Document: {source}\nClaim: {claim}\n\n"
    "Does the document explicitly CONTRADICT this claim? Answer YES or NO."
)


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ── DB helpers ─────────────────────────────────────────────────────────────────


def _get_turns_for_verify(limit: int = BATCH_LIMIT, turn_id: Optional[str] = None) -> List[Dict]:
    """Turns that completed extract but still need predicate verification.

    If turn_id is given, re-verify that specific turn (skips NOT EXISTS filter).

    Note: psql_json wraps in SELECT row_to_json(r) FROM (...) r, which breaks
    if the WITH clause contains UPDATE (PostgreSQL restriction). So we run the
    batch query via subprocess directly with inline row_to_json.
    """
    if turn_id:
        sql = (
            "SELECT t.id, t.user_turn, t.thinking, t.text, "
            "  t.detected_lang, t.created_at::text, t.est_chars "
            "FROM turns t "
            f"WHERE t.id = '{esc_sql(turn_id)}'::uuid "
            "  AND t.pipeline_state IN ('extracted', 'verifying')"
        )
        rows = psql_json(sql) or []
    else:
        sql = f"""
            WITH claimable AS (
                SELECT t.id
                FROM turns t
                WHERE t.pipeline_state = 'extracted'
                  AND EXISTS (
                      SELECT 1 FROM review_facts rf
                      WHERE rf.turn_id = t.id
                        AND rf.fact_type = 'text'
                        AND rf.source = 'extract_pipeline'
                  )
                LIMIT {limit}
                FOR UPDATE OF t SKIP LOCKED
            ),
            claimed AS (
                UPDATE turns SET pipeline_state = 'verifying'
                FROM claimable WHERE turns.id = claimable.id
                RETURNING turns.id
            ),
            result AS (
                SELECT t.id, t.user_turn, t.thinking, t.text,
                       t.detected_lang, t.created_at::text, t.est_chars
                FROM turns t
                WHERE t.id IN (SELECT id FROM claimed)
                ORDER BY t.est_chars ASC NULLS LAST, t.created_at ASC
            )
            SELECT row_to_json(r) FROM result r
        """
        try:
            r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
                return []
            rows = []
            for line in r.stdout.strip().split("\n"):
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        except Exception as e:
            print(f"  SQL ERROR: {e}")
            return []
    for r in rows:
        r["est_chars"] = r.get("est_chars") or 0
    return rows[:limit]


def _get_predicates(turn_id: str) -> List[Dict]:
    """Get all extracted predicates for a turn, ordered by fact_index."""
    rows = psql_json(f"""
        SELECT id::text, fact_index, evidence::text,
               verdict, nli_verdict, extract_model
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
          AND fact_type = 'text'
          AND source = 'extract_pipeline'
        ORDER BY fact_index
    """)
    return rows or []


def _update_predicate_nli(pred_id: str, nli_verdict: str) -> bool:
    """Update a single predicate's NLI verdict from Veritas."""
    return psql_ok(f"""
        UPDATE review_facts
        SET nli_verdict = '{esc_sql(nli_verdict)}',
            nli_llm = 'veritas-8b-fact-checker',
            verify_model = 'day-verifier'
        WHERE id = '{esc_sql(pred_id)}'::uuid
    """)


# ── Veritas NLI ────────────────────────────────────────────────────────────


def _chunk_source(text: str, max_chars: int = 3500) -> List[str]:
    """Split source text into chunks fitting Veritas ctx=4096.

    Reserves ~600 chars per chunk for the claim + formatting.
    Breaks at paragraph/sentence boundaries when possible.
    """
    if not text:
        return [""]
    if len(text) <= max_chars:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end >= len(text):
            chunks.append(text[start:])
            break
        search_start = max(start, end - 200)
        break_point = -1
        for sep in ["\n\n", ". ", ".\n", "! ", "? "]:
            idx = text.rfind(sep, search_start, end)
            if idx > break_point:
                break_point = idx + len(sep)
        if break_point > start:
            chunks.append(text[start:break_point])
            start = break_point
        else:
            chunks.append(text[start:end])
            start = end
    return chunks


def _calc_verify_timeout(source_len: int, gen_tokens: int = 2) -> int:
    """Scale timeout by input length + gen tokens. Same formula as extract_llm."""
    return min(
        _VERIFY_TIMEOUT_BASE
        + int(source_len * _VERIFY_TIMEOUT_PER_CHAR)
        + int(gen_tokens * _VERIFY_TIMEOUT_PER_TOK)
        + _VERIFY_GEN_TIME_BUF,
        _VERIFY_TIMEOUT_CAP,
    )


def _binary_nli_batch(
    source: str, predicates: List[Dict], model_key: str = "day_verify"
) -> List[Tuple[int, str]]:
    """Pass 1: Chunk-aware binary NLI — MAX over chunks (MiniCheck max_j).

    Splits source into ~3500c chunks (fits Veritas ctx=4096 with claim ~500c).
    Each predicate checked against ALL chunks independently.
    If ANY chunk returns YES → GROUNDED (MiniCheck max_j M(D_i,j, c_i)).
    Only if ALL chunks return NO → PASS2 (→ contradiction check).
    """
    if not predicates:
        return []

    chunks = _chunk_source(source)
    log(f"      {len(chunks)} chunk(s) — {sum(len(c) for c in chunks)} total chars")

    results: List[Tuple[int, str]] = []

    for i, pred in enumerate(predicates):
        claim = pred.get("evidence", "")[:500]
        verdict = "PASS2"  # default — all chunks must fail to stay PASS2

        for ci, chunk in enumerate(chunks):
            prompt = _VERITAS_PROMPT_BINARY.format(source=chunk, claim=claim)
            messages = [{"role": "user", "content": prompt}]
            try:
                resp = call_llm_with_retry(
                    messages,
                    model=model_key,
                    max_tokens=2,
                    temperature=0.0,
                    timeout=_calc_verify_timeout(len(chunk)),
                )
                s = resp.strip().upper()
                if s.startswith("YES"):
                    verdict = "GROUNDED"
                    if ci > 0:
                        log(f"        p{i} chunk {ci}: YES → GROUNDED")
                    break  # MAX over chunks — first YES wins
                elif s.startswith("NO"):
                    continue  # try next chunk
                else:
                    verdict = "AMBIGUOUS"
                    break  # non-parse: skip to next predicate
            except Exception as e:
                log(f"      predicate {i} chunk {ci} error: {e}")
                verdict = "AMBIGUOUS"
                break

        results.append((i, verdict))

    n_g = sum(1 for _, v in results if v == "GROUNDED")
    log(f"      {n_g}/{len(predicates)} grounded (chunk-aware MAX)")

    return results


def _contradiction_check(source: str, claim: str, model_key: str = "day_verify") -> str:
    """Pass 2: Chunk-aware CONTRADICTION check — MAX over chunks.

    If ANY chunk signals contradiction → CONTRADICTION.
    Only if ALL chunks say NO or timeout → UNGROUNDED.
    """
    chunks = _chunk_source(source)
    overall = "UNGROUNDED"

    for ci, chunk in enumerate(chunks):
        prompt = _VERITAS_PROMPT_CONTRADICT.format(source=chunk, claim=claim[:500])
        messages = [{"role": "user", "content": prompt}]
        try:
            resp = call_llm_with_retry(
                messages,
                model=model_key,
                max_tokens=2,
                temperature=0.0,
                timeout=_calc_verify_timeout(len(chunk)),
            )
            s = resp.strip().upper()
            if s.startswith("YES"):
                return "CONTRADICTION"  # MAX — any chunk contradicts
            elif s.startswith("NO"):
                continue  # try next chunk
            else:
                overall = "AMBIGUOUS"
                break
        except Exception as e:
            log(f"      contradiction chunk {ci} error: {e}")
            overall = "AMBIGUOUS"
            break

    return overall


# ── Per-turn processing ────────────────────────────────────────────────────


def _verify_turn(
    turn: Dict, model_key: str = "day_verify"
) -> Tuple[str, List[Dict], Optional[str]]:
    """Verify all predicates for one turn. Returns (turn_id, updates, error).

    updates: list of {id, fact_index, verdict}
    model_key: 'day_verify' (:8082) or 'day_verify_b' (:8083)
    """
    try:
        turn_id = turn["id"]
        predicates = _get_predicates(turn_id)
        if not predicates:
            return (turn_id, [], None)

        user_turn = turn.get("user_turn", "") or ""
        thinking = turn.get("thinking", "") or ""
        text = turn.get("text", "") or ""
        source_text = " ".join(f"{user_turn}\n{text}".split())

        # Pass 1: Binary NLI (all predicates batched)
        binary = _binary_nli_batch(source_text, predicates, model_key=model_key)

        # Pass 2: CONTRADICTION check for PASS2 items
        updates = []
        for idx, verdict in binary:
            pred = predicates[idx]
            if verdict == "PASS2":
                final = _contradiction_check(
                    source_text, pred.get("evidence", ""), model_key=model_key
                )
            else:
                final = verdict
            updates.append(
                {
                    "id": pred["id"],
                    "fact_index": pred["fact_index"],
                    "verdict": final,
                    "old_nli": pred.get("nli_verdict", ""),
                }
            )

        return (turn_id, updates, None)
    except Exception as e:
        return (turn["id"], [], f"{type(e).__name__}: {e}")


# ── Pipeline ───────────────────────────────────────────────────────────────


def day_verify_pipeline(
    limit: int = BATCH_LIMIT,
    dry_run: bool = False,
    turn_id: Optional[str] = None,
    model_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Verify extracted predicates against source using Veritas two-pass NLI."""
    t_start = time.monotonic()
    processed = 0
    verified_count = 0
    contradicted = 0
    ungrounded = 0
    ambiguous = 0
    failed = 0

    if model_keys is None:
        model_keys = ["day-verify"]

    n_workers = min(PARALLEL, len(model_keys))

    heartbeat("day_verify", "pipeline_start")

    log("=" * 60)
    log("Day Verify — predicate NLI (Veritas-8B Fact Checker)")
    if dry_run:
        log("  [DRY RUN] No writes to DB")
    if turn_id:
        log(f"  [re-verify] turn_id={turn_id[:12]}")
    log("=" * 60)

    turns = _get_turns_for_verify(limit, turn_id=turn_id)
    if not turns:
        log("[done] No turns needing verification")
        return {"ok": True, "processed": 0, "elapsed_s": 0}

    solo_turns = [t for t in turns if t.get("est_chars", 0) > SOLO_THRESHOLD]
    pool_turns = [t for t in turns if t.get("est_chars", 0) <= SOLO_THRESHOLD]
    log(
        f"Processing {len(turns)} turn(s): {len(solo_turns)} solo (>{SOLO_THRESHOLD} chars), "
        f"{len(pool_turns)} parallel (max_workers={PARALLEL})"
    )

    # ── Processing ────────────────────────────────────────────────────────
    turn_results: Dict[str, Tuple[List[Dict], Optional[str]]] = {}
    llm_t0 = time.monotonic()

    # Round-robin model assignment across turns
    n_keys = len(model_keys)

    if pool_turns:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            fut_map = {}
            for i, turn in enumerate(pool_turns):
                mk = model_keys[i % n_keys]
                fut = pool.submit(_verify_turn, turn, mk)
                fut_map[fut] = turn
            for fut in as_completed(fut_map):
                trn = fut_map[fut]
                _, updates, error = fut.result()
                turn_results[trn["id"]] = (updates, error)

    for i, turn in enumerate(solo_turns):
        mk = model_keys[i % n_keys]
        _, updates, error = _verify_turn(turn, mk)
        turn_results[turn["id"]] = (updates, error)

    log(f"  processing phase: {time.monotonic() - llm_t0:.1f}s")

    # ── Store results ─────────────────────────────────────────────────────
    for turn in turns:
        turn_id_val = turn["id"]
        turn_short = turn_id_val[:8]
        log(f"\n  [{turn_short}]")

        updates, error = turn_results.get(turn_id_val, ([], "missing batch result"))

        if error:
            log(f"    ERROR: {error}")
            failed += 1
            continue

        if not updates:
            log("    No predicates — skip")
            # Still advance state — nothing to verify is OK
            if not dry_run:
                psql_ok(
                    f"UPDATE turns SET pipeline_state = 'verified' "
                    f"WHERE id = '{esc_sql(turn_id_val)}'::uuid"
                )
            processed += 1
            continue

        # Log per-verdict counts
        n_g = sum(1 for u in updates if u["verdict"] == "GROUNDED")
        n_c = sum(1 for u in updates if u["verdict"] == "CONTRADICTION")
        n_u = sum(1 for u in updates if u["verdict"] == "UNGROUNDED")
        n_a = sum(1 for u in updates if u["verdict"] == "AMBIGUOUS")
        log(f"    predicates: {len(updates)} total (G={n_g} C={n_c} U={n_u} A={n_a})")

        # Show changed verdicts
        changed = [u for u in updates if u["verdict"] != u["old_nli"]]
        if changed:
            for ch in changed[:5]:
                old = ch.get("old_nli", "") or "NONE"
                log(f"      fi={ch['fact_index']}: {old} → {ch['verdict']}")
            if len(changed) > 5:
                log(f"      ... and {len(changed) - 5} more")

        if dry_run:
            log("    [DRY] Would update nli_verdict")
            processed += 1
            continue

        # Batch UPDATE nli_verdict per predicate
        for upd in updates:
            _update_predicate_nli(upd["id"], upd["verdict"])

        # Advance turn state
        psql_ok(
            f"UPDATE turns SET pipeline_state = 'verified' "
            f"WHERE id = '{esc_sql(turn_id_val)}'::uuid"
        )
        log(f"    {len(updates)} predicates updated → verified")

        verified_count += n_g
        contradicted += n_c
        ungrounded += n_u
        ambiguous += n_a
        processed += 1
        heartbeat("day_verify", f"turn {turn_id_val[:8]} verified")

    elapsed = round(time.monotonic() - t_start, 1)
    log(f"\n{'=' * 60}")
    log(
        f"Done: {processed} turns, "
        f"{verified_count}G/{contradicted}C/{ungrounded}U/{ambiguous}A "
        f"({elapsed}s)"
    )
    if failed:
        log(f"  FAILED: {failed} turns")
    log(f"{'=' * 60}")

    return {
        "ok": failed == 0,
        "processed": processed,
        "failed": failed,
        "elapsed_s": elapsed,
        "verdicts": {
            "grounded": verified_count,
            "contradiction": contradicted,
            "ungrounded": ungrounded,
            "ambiguous": ambiguous,
        },
    }


def _launch_reranker() -> bool:
    """Launch reranker (Qwen3-Reranker-4B-Q8) on :8080 via podman exec."""
    import subprocess

    reranker = MODEL_METADATA["reranker"]
    cmd = [
        "podman",
        "exec",
        "-d",
        "devforge-inference",
        "taskset",
        "-c",
        "0-3",
        "/app/llama-server",
        "-m",
        f"/models/{reranker['file']}",
        "--host",
        "0.0.0.0",
        "--port",
        "8080",
        "--ctx-size",
        str(reranker.get("ctx", 2048)),
        "--batch-size",
        str(reranker.get("batch_size", 256)),
        "--ubatch-size",
        str(reranker.get("ubatch_size", 256)),
        "--threads",
        str(reranker.get("threads", 4)),
        "--threads-batch",
        str(reranker.get("threads_batch", 4)),
        "--no-mmap",
        "-lv",
        "6",
    ]
    log("  launching reranker on :8080 via podman exec")
    r = subprocess.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        log(f"  reranker launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False
    ok = wait_health(8080, timeout=300)
    if ok:
        log("  reranker :8080 healthy")
    else:
        log("  reranker :8080 health timeout")
    return ok


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Day Verify — predicate NLI with Veritas-8B")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--turn-id", type=str, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["q8", "q4", "q8-dual"],
        default="q8",
        help="'q8' = single :8082 Q8_0 parallel=2 (default), 'q4' = dual :8082+:8083 Q4_K_M, 'q8-dual' = dual Q8_0",
    )
    args = parser.parse_args()

    if args.mode == "q4":
        ensure_dual("day-verifier-q4", "day-verifier-q4-b")
        preflight_checks("day_verify.py", required_ports={8082, 8083})
        result = day_verify_pipeline(
            limit=args.limit,
            dry_run=args.dry_run,
            turn_id=args.turn_id,
            model_keys=["day_verify_q4", "day_verify_q4_b"],
        )
    elif args.mode == "q8-dual":
        ensure_dual("day-verifier-q8", "day-verifier-q8-b")
        preflight_checks("day_verify.py", required_ports={8082, 8083})
        result = day_verify_pipeline(
            limit=args.limit,
            dry_run=args.dry_run,
            turn_id=args.turn_id,
            model_keys=["day_verify_q8", "day_verify_q8_b"],
        )
    else:  # q8 (default)
        ensure_model("day-verifier")
        preflight_checks("day_verify.py", required_ports={8082})
        result = day_verify_pipeline(
            limit=args.limit,
            dry_run=args.dry_run,
            turn_id=args.turn_id,
        )

    if args.dry_run:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    from lib.llm_client import recall_tiny

    recall_tiny()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
