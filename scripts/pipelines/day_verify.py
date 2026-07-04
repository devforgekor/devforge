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
import re
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
from lib.pod_manager import ensure_model
from lib.watchdog.messenger import heartbeat

BATCH_LIMIT = 50
PARALLEL = 2
SOLO_THRESHOLD = 5000

# Veritas was fine-tuned on MiniCheck bespoke format (Document + Claim → YES/NO).
# We use the same format for Pass 1 (binary), then Pass 2 for NO→CONTRADICTION.
_VERITAS_PROMPT_BINARY = """Document:
{source}

Claims:
{claims}

For each claim, answer YES if the document supports it, NO otherwise.
Number each answer:

{claim_lines}"""

_VERITAS_PROMPT_CONTRADICT = """Document:
{source}

Claim: {claim}

Does the document explicitly CONTRADICT this claim? Answer YES or NO."""


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


def _extract_binary_answer(line: str, idx: int) -> str:
    """Parse a single line for YES/NO answer. Returns GROUNDED, PASS2, or AMBIGUOUS."""
    s = line.strip().upper()
    # Strip leading number prefix ("1. YES" → "YES")
    if s.startswith(f"{idx}."):
        s = s.split(".", 1)[1].strip()
    # Strip trailing punctuation or labels
    for sep in (" -", " —", "|", "("):
        if sep in s:
            s = s.split(sep)[0].strip()
    if s.startswith("YES") or "YES" in s.split()[:1]:
        return "GROUNDED"
    elif s.startswith("NO") or "NO" in s.split()[:1]:
        return "PASS2"
    return "AMBIGUOUS"


def _chunk_text(text: str, max_chars: int = 800) -> List[str]:
    """Split text into ~max_chars chunks at sentence/paragraph boundaries.

    Same approach as extract_llm._split_atomic — preserves semantic units,
    no overlap needed, small fragments merged into previous chunk.
    """
    if len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n\s*\n", text)
    chunks = []
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            chunks.append(para)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        current = ""
        for sent in sentences:
            if len(current) + len(sent) + 1 <= max_chars:
                current = (current + " " + sent).strip()
            else:
                if current:
                    chunks.append(current)
                current = sent
        if current:
            chunks.append(current)
    merged = []
    for c in chunks:
        if merged and len(c) < 40:
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


def _binary_nli_batch(source: str, predicates: List[Dict]) -> List[Tuple[int, str]]:
    """Pass 1: Binary YES/NO across chunked source (~800c each).

    Each chunk evaluated independently. A predicate is GROUNDED if
    ANY chunk finds it supported.
    """
    if not predicates:
        return []

    chunks = _chunk_text(source, max_chars=800)
    claims_lines = "\n".join(
        f"{i}. {p.get('evidence', '')[:300]}" for i, p in enumerate(predicates, 1)
    )
    chunk_votes: List[List[str]] = [[] for _ in range(len(predicates))]

    for ci, chunk in enumerate(chunks):
        answer_lines = "\n".join(f"{i}." for i in range(1, len(predicates) + 1))
        prompt = _VERITAS_PROMPT_BINARY.format(
            source=chunk,
            claims=claims_lines,
            claim_lines=answer_lines,
        )

        try:
            resp = call_llm_with_retry(
                [{"role": "user", "content": prompt}],
                model="day_verify",
                max_tokens=len(predicates) * 8 + 16,
                temperature=0.0,
                timeout=120,
            )
            lines = resp.strip().split("\n")
            for i in range(len(predicates)):
                matched = False
                for line in lines:
                    if line.strip().startswith(f"{i + 1}.") or line.strip().startswith(f"{i + 1}:"):
                        chunk_votes[i].append(_extract_binary_answer(line, i + 1))
                        matched = True
                        break
                if not matched:
                    if i < len(lines):
                        chunk_votes[i].append(_extract_binary_answer(lines[i], i + 1))
                    else:
                        chunk_votes[i].append("AMBIGUOUS")
        except Exception as e:
            log(f"      chunk {ci} NLI error: {e}")
            for i in range(len(predicates)):
                chunk_votes[i].append("AMBIGUOUS")

    # Aggregate: YES from any chunk -> GROUNDED
    # All NO -> PASS2 (check CONTRADICTION)
    # Mixed -> AMBIGUOUS
    results = []
    for i in range(len(predicates)):
        votes = chunk_votes[i]
        yes_count = sum(1 for v in votes if v == "GROUNDED")
        no_count = sum(1 for v in votes if v == "PASS2")
        if yes_count > 0:
            results.append((i, "GROUNDED"))
        elif no_count == len(votes):
            results.append((i, "PASS2"))
        else:
            results.append((i, "AMBIGUOUS"))

    if len(chunks) > 1:
        n_yes = sum(1 for _, v in results if v == "GROUNDED")
        log(f"      {len(chunks)} chunks, {n_yes}/{len(predicates)} grounded")

    return results


def _contradiction_check(source: str, claim: str) -> str:
    """Pass 2: Determine if source contradicts claim (chunked source)."""
    chunks = _chunk_text(source, max_chars=800)
    votes = []
    for chunk in chunks:
        prompt = _VERITAS_PROMPT_CONTRADICT.format(source=chunk, claim=claim[:500])
        try:
            resp = call_llm_with_retry(
                [{"role": "user", "content": prompt}],
                model="day_verify",
                max_tokens=8,
                temperature=0.0,
                timeout=30,
            )
            s = resp.strip().upper()
            if s.startswith("YES") or "YES" in s.split()[:1]:
                votes.append("CONTRADICTION")
            elif s.startswith("NO") or "NO" in s.split()[:1]:
                votes.append("UNGROUNDED")
            else:
                votes.append("AMBIGUOUS")
        except Exception as e:
            log(f"      contradiction chunk error: {e}")
            votes.append("AMBIGUOUS")

    # Any chunk says CONTRADICTION -> CONTRADICTION
    # All UNGROUNDED -> UNGROUNDED
    # Mixed -> AMBIGUOUS
    if any(v == "CONTRADICTION" for v in votes):
        return "CONTRADICTION"
    if all(v == "UNGROUNDED" for v in votes):
        return "UNGROUNDED"
    return "AMBIGUOUS"


# ── Per-turn processing ────────────────────────────────────────────────────


def _verify_turn(turn: Dict) -> Tuple[str, List[Dict], Optional[str]]:
    """Verify all predicates for one turn. Returns (turn_id, updates, error).

    updates: list of {id, fact_index, verdict}
    """
    try:
        turn_id = turn["id"]
        predicates = _get_predicates(turn_id)
        if not predicates:
            return (turn_id, [], None)

        user_turn = turn.get("user_turn", "") or ""
        thinking = turn.get("thinking", "") or ""
        text = turn.get("text", "") or ""
        # Truncate to 4000 chars: Veritas ctx=4096, chunked to 800c + claims ~500c
        source_text = " ".join(f"{user_turn}\n{thinking}\n{text}".split())[:4000]

        # Pass 1: Binary NLI (all predicates batched)
        binary = _binary_nli_batch(source_text, predicates)

        # Pass 2: CONTRADICTION check for PASS2 items
        updates = []
        for idx, verdict in binary:
            pred = predicates[idx]
            if verdict == "PASS2":
                final = _contradiction_check(source_text, pred.get("evidence", ""))
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
    limit: int = BATCH_LIMIT, dry_run: bool = False, turn_id: Optional[str] = None
) -> Dict[str, Any]:
    """Verify extracted predicates against source using Veritas two-pass NLI."""
    t_start = time.monotonic()
    processed = 0
    verified_count = 0
    contradicted = 0
    ungrounded = 0
    ambiguous = 0
    failed = 0

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

    if pool_turns:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            fut_map = {}
            for turn in pool_turns:
                fut = pool.submit(_verify_turn, turn)
                fut_map[fut] = turn
            for fut in as_completed(fut_map):
                trn = fut_map[fut]
                _, updates, error = fut.result()
                turn_results[trn["id"]] = (updates, error)

    for turn in solo_turns:
        _, updates, error = _verify_turn(turn)
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


def main() -> None:
    ensure_model("day-verifier", skip_if_healthy=True)
    preflight_checks("day_verify.py", required_ports={8082})
    import argparse

    parser = argparse.ArgumentParser(description="Day Verify — predicate NLI with Veritas-8B")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument(
        "--turn-id",
        type=str,
        default=None,
        help="Re-verify a specific turn UUID",
    )
    parser.add_argument("--dry-run", action="store_true", help="Simulate without DB writes")
    args = parser.parse_args()

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
