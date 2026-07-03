#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh — Extract phase (after polish, via day_cycle.py)
"""Extract Pipeline — state-based fact extraction via NOT EXISTS anti-join.

SSOT: turns table. Filters unprocessed turns using NOT EXISTS against review_facts.
Each cycle: SELECT WHERE NOT EXISTS → section-major extraction → verify → store.
Failed turns are retried on next cycle (no checkpoint to advance past them).
DB UNIQUE (turn_id, fact_index, extract_model) prevents duplicate storage.

Extraction: solo section-major (all turns via _extract_solo_section_major).
  user batch → text batch (thinking as context only, per F-CoT).
  KV cache: system prompt identical per section, warmup 1 then cached for rest.

Submodules:
  extract_llm.py   — LLM interaction, prompts, section-major extraction
  extract_verify.py — NLI verify, reranker, post-processing, fallback

Flow:
  Phase 1: SELECT unprocessed (NOT EXISTS review_facts WHERE source=extract_pipeline, limit 50)
  Phase 2: Solo section-major extraction (user batch → text batch)
  Phase 3: Post-process cleanup (dedup, short filter, markdown)
  Phase 4: LLM NLI self-verify — ENTAILMENT=grounded, CONTRADICTION=drop, NEUTRAL→reranker
  Phase 5: Reranker faithfulness check (inference :8080) — only NEUTRAL items
  Phase 6: Handle failures — retry or mark
  Phase 7: Store to review_facts + enqueue

Usage:
  python3 scripts/pipelines/extract.py                          # batch from state
  python3 scripts/pipelines/extract.py --turn-id <uuid>         # single turn (debug)
  python3 scripts/pipelines/extract.py --limit 50               # batch cap
  python3 scripts/pipelines/extract.py --dry-run                # simulate, no writes
"""

import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from extract_llm import (
    _SIGTERM_RECEIVED,
    SYSTEM_DESCRIBE_FILE,
    _checkpoint_sections,
    _parse_json,
    _sigterm_handler,
)
from extract_llm import (
    _extract_edcr_freeform as _extract_solo_section_major,
)
from extract_verify import (
    _llm_nli_verify,
    _post_process_extractions,
    _refine_batch,
    _verify_extractions,
)
from lib.db import esc_sql, psql_json, psql_ok
from lib.infra.preflight import preflight_checks
from lib.llm_client import call_llm
from lib.pod_manager import ensure_model as _ensure_model_pod
from lib.watchdog.messenger import heartbeat

BATCH_LIMIT = 50
PARALLEL = 2


# ── Phase 7 Store — review_facts INSERT / UPDATE ───────────────────────


def _insert_fact(
    turn_id: str,
    fact_index: int,
    fact_type: str,
    evidence: str,
    extract_model: str,
    prompt_tokens: Optional[int] = None,
    gen_tokens: Optional[int] = None,
    elapsed_ms: Optional[float] = None,
    faithful_score: Optional[float] = None,
    faithful_method: Optional[str] = None,
    grounding: Optional[str] = None,
    nli_llm: Optional[str] = None,
    source_file: Optional[str] = None,
    corrected_evidence: Optional[str] = None,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    object_: Optional[str] = None,
    qualifiers: Optional[dict] = None,
) -> bool:
    cols = [
        "turn_id",
        "fact_index",
        "fact_type",
        "evidence",
        "extract_model",
        "verdict",
        "source",
        "fact_action",
    ]
    vals = [
        f"'{esc_sql(turn_id)}'::uuid",
        str(fact_index),
        f"'{esc_sql(fact_type)}'",
        f"'{esc_sql(evidence[:5000])}'",
        f"'{esc_sql(extract_model)}'",
        "'passed'",
        "'extract_pipeline'",
        "'extracted'",
    ]
    set_clauses = []
    if prompt_tokens is not None:
        cols.append("prompt_tokens")
        vals.append(str(prompt_tokens))
        set_clauses.append("prompt_tokens = EXCLUDED.prompt_tokens")
    if gen_tokens is not None:
        cols.append("gen_tokens")
        vals.append(str(gen_tokens))
        set_clauses.append("gen_tokens = EXCLUDED.gen_tokens")
    if faithful_score is not None:
        cols.append("faithful_score")
        vals.append(f"{faithful_score:.1f}")
        set_clauses.append(f"faithful_score = {faithful_score:.1f}")
    if faithful_method:
        cols.append("faithful_method")
        vals.append(f"'{esc_sql(faithful_method)}'")
        set_clauses.append(f"faithful_method = '{esc_sql(faithful_method)}'")
    if elapsed_ms is not None:
        cols.append("elapsed_ms")
        vals.append(f"{elapsed_ms:.1f}")
        set_clauses.append(f"elapsed_ms = {elapsed_ms:.1f}")
    if grounding:
        cols.append("nli_verdict")
        vals.append(f"'{esc_sql(grounding)}'")
        set_clauses.append(f"nli_verdict = '{esc_sql(grounding)}'")
    if nli_llm:
        cols.append("nli_llm")
        vals.append(f"'{esc_sql(nli_llm)}'")
        set_clauses.append(f"nli_llm = '{esc_sql(nli_llm)}'")
    if source_file:
        cols.append("source_file")
        vals.append(f"'{esc_sql(source_file)}'")
        set_clauses.append(f"source_file = '{esc_sql(source_file)}'")
    if corrected_evidence:
        cols.append("corrected_evidence")
        vals.append(f"'{esc_sql(corrected_evidence[:5000])}'")
        set_clauses.append(f"corrected_evidence = '{esc_sql(corrected_evidence[:5000])}'")
    if subject:
        cols.append("subject")
        vals.append(f"'{esc_sql(subject)}'")
        set_clauses.append(f"subject = '{esc_sql(subject)}'")
    if predicate:
        cols.append("predicate")
        vals.append(f"'{esc_sql(predicate)}'")
        set_clauses.append(f"predicate = '{esc_sql(predicate)}'")
    if object_:
        cols.append("object")
        vals.append(f"'{esc_sql(object_)}'")
        set_clauses.append(f"object = '{esc_sql(object_)}'")
    if qualifiers:
        qjson = json.dumps(qualifiers).replace("'", "''")
        cols.append("qualifiers")
        vals.append(f"'{qjson}'::jsonb")
        set_clauses.append(f"qualifiers = '{qjson}'::jsonb")

    sql = (
        f"INSERT INTO review_facts ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)}) "
        f"ON CONFLICT (turn_id, fact_index, extract_model) "
        f"DO UPDATE SET evidence = EXCLUDED.evidence"
        + (f", {', '.join(set_clauses)}" if set_clauses else "")
    )
    return psql_ok(sql)


def _insert_mark(turn_id: str, mark: str, extract_model: str, is_final: bool = False) -> bool:
    source = "extract_pipeline" if is_final else "extract_marker"
    sql = (
        "INSERT INTO review_facts "
        "(turn_id, fact_index, fact_type, evidence, extract_model, verdict, "
        " source, reason, fact_action, fact_confidence) "
        "VALUES ("
        f"'{esc_sql(turn_id)}'::uuid, "
        f"(SELECT COALESCE(MAX(fact_index), -1) + 1 "
        f" FROM review_facts WHERE turn_id = '{esc_sql(turn_id)}'::uuid), "
        f"'marker', '{esc_sql(mark)}', "
        f"'{esc_sql(extract_model)}', 'system', "
        f"'{source}', 'tracking_marker', 'marked', 0"
        ")"
    )
    return psql_ok(sql)


def _insert_noise_marker(turn_id: str) -> bool:
    """Insert a noise_marker fact — blocks re-extraction via NOT EXISTS.

    pipeline_state stays 'scanned'. User must confirm via Telegram:
      - CONFIRM → turn skipped (pipeline_state='verified')
      - REJECT  → marker deleted → next cycle re-extracts
    """
    sql = (
        "INSERT INTO review_facts "
        "(turn_id, fact_index, fact_type, evidence, extract_model, verdict, "
        " source, fact_action, fact_confidence) "
        "VALUES ("
        f"'{esc_sql(turn_id)}'::uuid, "
        f"(SELECT COALESCE(MAX(fact_index), -1) + 1 "
        f" FROM review_facts WHERE turn_id = '{esc_sql(turn_id)}'::uuid), "
        f"'noise_marker', 'noise skip - pending user review', "
        f"'day_extract', 'pending', "
        f"'extract_pipeline', 'noise', 0"
        ")"
    )
    return psql_ok(sql)


# ── Checkpoint (WAL — Write-Ahead Log) ─────────────────────────────────

_CHECKPOINT_DDL = """
CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
    pipeline TEXT NOT NULL,
    turn_id UUID NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (pipeline, turn_id)
)
"""


def _ensure_checkpoint_table() -> None:
    psql_ok(_CHECKPOINT_DDL)


def _save_checkpoint(turn_id: str, extractions: List[Dict]) -> None:
    data_str = json.dumps(extractions, ensure_ascii=False)
    psql_ok(
        "INSERT INTO pipeline_checkpoints (pipeline, turn_id, data) "
        f"VALUES ('extract', '{esc_sql(turn_id)}'::uuid, '{esc_sql(data_str)}'::jsonb) "
        "ON CONFLICT (pipeline, turn_id) DO UPDATE "
        "SET data = EXCLUDED.data, created_at = NOW()"
    )


def _load_checkpoint(turn_id: str) -> Optional[List[Dict]]:
    rows = psql_json(
        f"SELECT data FROM pipeline_checkpoints "
        f"WHERE pipeline = 'extract' AND turn_id = '{esc_sql(turn_id)}'::uuid"
    )
    if rows and rows[0].get("data"):
        return rows[0]["data"]
    return None


def _delete_checkpoint(turn_id: str) -> None:
    psql_ok(
        f"DELETE FROM pipeline_checkpoints "
        f"WHERE pipeline = 'extract' AND turn_id = '{esc_sql(turn_id)}'::uuid"
    )


# ── Phase 1: Select turns ──────────────────────────────────────────────


def _get_unprocessed_turns(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Atomically claim scanned turns via FOR UPDATE SKIP LOCKED,
    set pipeline_state='extracting', and return turn data.
    Prevents duplicate processing when multiple workers run concurrently.
    """
    sql = f"""
        WITH claimable AS (
            SELECT t.id
            FROM turns t
            WHERE t.text != ''
              AND NOT EXISTS (
                SELECT 1 FROM review_facts rf
                WHERE rf.turn_id = t.id AND rf.source = 'extract_pipeline'
              )
              AND t.pipeline_state = 'scanned'
            ORDER BY t.est_chars ASC NULLS LAST, t.created_at DESC
            LIMIT {limit}
            FOR UPDATE SKIP LOCKED
        ),
        claimed AS (
            UPDATE turns SET pipeline_state = 'extracting'
            FROM claimable
            WHERE turns.id = claimable.id
            RETURNING turns.*
        )
        SELECT
          claimed.id,
          COALESCE(claimed.user_turn_clean, claimed.user_turn_clean_polished, claimed.user_turn) AS user_turn,
          COALESCE(claimed.thinking_clean, claimed.thinking_clean_polished, claimed.thinking) AS thinking,
          COALESCE(claimed.text_clean, claimed.text_clean_polished, claimed.text) AS text,
          claimed.text_clean,
          claimed.thinking_clean,
          claimed.detected_lang,
          LENGTH(COALESCE(claimed.user_turn, '')) AS user_raw_len,
          LENGTH(COALESCE(claimed.text, '')) AS text_raw_len,
          LENGTH(COALESCE(claimed.thinking, '')) AS think_raw_len,
          claimed.source_message_id,
          claimed.created_at,
          claimed.conversation_id,
          claimed.seq,
          claimed.est_chars
        FROM claimed
        ORDER BY claimed.est_chars ASC NULLS LAST, claimed.created_at DESC
    """
    rows = psql_json(sql)
    if not rows:
        return []
    turns = []
    for row in rows:
        turns.append(
            {
                "id": row.get("id", ""),
                "user_turn": row.get("user_turn", ""),
                "thinking": row.get("thinking") or None,
                "text": row.get("text", ""),
                "text_clean_orig": row.get("text_clean") or "",
                "thinking_clean_orig": row.get("thinking_clean") or "",
                "detected_lang": row.get("detected_lang"),
                "user_raw_len": row.get("user_raw_len", 0) or 0,
                "text_raw_len": row.get("text_raw_len", 0) or 0,
                "think_raw_len": row.get("think_raw_len", 0) or 0,
                "source_message_id": row.get("source_message_id", ""),
                "created_at": row.get("created_at", ""),
                "conversation_id": row.get("conversation_id", ""),
                "seq": row.get("seq", 0) or 0,
                "est_chars": row.get("est_chars", 0) or 0,
            }
        )
    return turns


# ── Main pipeline ─────────────────────────────────────────────────────


def extract_pipeline(
    turn_id: Optional[str] = None,
    limit: int = BATCH_LIMIT,
    dry_run: bool = False,
    pulse_context: Optional[str] = None,
) -> Dict[str, Any]:
    t_start = time.monotonic()
    heartbeat("day_extract", "pipeline_start")

    print(f"\n{'=' * 60}")
    print("Extract Pipeline — LLM extract → reranker verify → store")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    psql_ok("ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm TEXT")

    if not dry_run:
        _ensure_checkpoint_table()

    # ── Phase 1: Select turns ─────────────────────────────────────
    if turn_id:
        sql = (
            "SELECT t.id, t.user_turn, t.thinking, t.text, "
            "  t.source_message_id, t.created_at, "
            "  t.conversation_id, t.seq, t.detected_lang "
            f"FROM turns t WHERE t.id = '{esc_sql(turn_id)}'::uuid"
        )
        rows = psql_json(sql)
        if not rows:
            print(f"[extract] Turn not found: {turn_id}")
            return {"processed": 0, "failed": 1, "facts": 0, "ok": False}
        r = rows[0]
        turns = [
            {
                "id": r["id"],
                "user_turn": r["user_turn"],
                "thinking": r.get("thinking") or None,
                "text": r["text"],
                "detected_lang": r.get("detected_lang"),
                "source_message_id": r.get("source_message_id", ""),
                "created_at": r["created_at"],
                "conversation_id": r["conversation_id"],
                "seq": r.get("seq", 0) or 0,
            }
        ]
    else:
        turns = _get_unprocessed_turns(limit)

    if not turns:
        print("[extract] No unprocessed turns found")
        return {"processed": 0, "failed": 0, "facts": 0, "ok": True}

    print(f"[extract] Processing {len(turns)} turn(s)", flush=True)

    total_facts = 0
    processed = 0
    failed = 0
    skipped_noise = 0
    _failures: list = []
    _noise: list = []

    # ── Phase 1: Solo section-major extraction (KV cache batch) ─────
    print(f"[extract] Processing {len(turns)} turn(s) via solo section-major...", flush=True)
    turn_results: Dict[str, Tuple] = {}
    llm_t0 = time.monotonic()

    if not dry_run:
        ckpt_skips = 0
        for t in turns:
            ckpt = _load_checkpoint(t["id"])
            if ckpt:
                if _checkpoint_sections(ckpt) >= {"user", "text"}:
                    turn_results[t["id"]] = (None, None)
                    ckpt_skips += 1
        if ckpt_skips:
            print(
                f"  [extract]   {ckpt_skips} turn(s) have complete checkpoint — skipping extraction",
                flush=True,
            )

    # Filter turns without checkpoint (those still need extraction)
    solo_turns = [t for t in turns if t["id"] not in turn_results]

    if solo_turns:
        print(f"  [solo] {len(solo_turns)} turns (section-major, KV cache)", flush=True)
        for turn, ex_result, error in _extract_solo_section_major(
            solo_turns, pulse_context, dry_run
        ):
            if error:
                print(f"  [solo] {turn['id'][:8]} — extraction failed: {error}", flush=True)
            turn_results[turn["id"]] = (ex_result, error)

    print(f"  [extract]   LLM calls: {time.monotonic() - llm_t0:.1f}s", flush=True)

    # ── Phase 2a: Error/noise handling + post_process ────────────
    nli_tasks = []

    idx = 0
    for turn in turns:
        idx += 1
        turn_id_val = turn["id"]
        user_turn = turn["user_turn"] or ""
        thinking = turn["thinking"] or ""
        text = turn["text"] or ""
        print(
            f"\n[{idx}/{len(turns)}] Turn {turn_id_val[:8]}... "
            f"user={len(user_turn)}ch think={len(thinking)}ch text={len(text)}ch"
        )

        try:
            ex_result, error = turn_results.get(turn_id_val, (None, "missing batch result"))
            if ex_result is None and error in (None, "missing batch result") and not dry_run:
                ckpt = _load_checkpoint(turn_id_val)
                if ckpt:
                    print(f"  Checkpoint resume — {len(ckpt)} raw facts", flush=True)
                    ex_result = {"extractions": ckpt, "usage": {}, "timings": {}}
                    error = None
                elif error == "missing batch result":
                    print("  [extract]   Skip — turn not processed (no checkpoint)", flush=True)
                    continue
            used_model = "day_extract"
            mark = ""

            if error == "noise skip":
                print(
                    "  [extract]   Noise skip (gibberish/API error) — noise_marker inserted",
                    flush=True,
                )
                if not dry_run:
                    _insert_noise_marker(turn_id_val)
                skipped_noise += 1
                _noise.append(
                    {
                        "turn_id": turn_id_val,
                        "reason": "noise skip",
                        "preview": (text or user_turn or "")[:100],
                    }
                )
                continue

            if error:
                print(f"  [extract]   day_extract exception: {error}", flush=True)
                mark = "추출 실패"
                if not dry_run:
                    _insert_mark(turn_id_val, mark, used_model, is_final=True)
                    psql_ok(
                        f"UPDATE turns SET pipeline_state = 'extracted' WHERE id = '{esc_sql(turn_id_val)}'::uuid"
                    )
                failed += 1
                _failures.append(
                    {
                        "turn_id": turn_id_val,
                        "reason": error,
                        "preview": (text or user_turn or "")[:100],
                    }
                )
                continue

            if ex_result is None:
                print("  [extract]   Parse failure")
                if not dry_run:
                    _insert_mark(turn_id_val, "추출 parse 실패", used_model, is_final=True)
                    psql_ok(
                        f"UPDATE turns SET pipeline_state = 'extracted' WHERE id = '{esc_sql(turn_id_val)}'::uuid"
                    )
                failed += 1
                _failures.append(
                    {
                        "turn_id": turn_id_val,
                        "reason": "추출 parse 실패",
                        "preview": (text or user_turn or "")[:100],
                    }
                )
                continue

            raw_ex = ex_result["extractions"]
            ex_usage = ex_result.get("usage", {})
            ex_timings = ex_result.get("timings", {})
            ex_elapsed = ex_result.get("elapsed_ms", 0)
            verified = _post_process_extractions(raw_ex, turn_id_val, user_turn, thinking, text)
            nli_tasks.append(
                (
                    turn_id_val,
                    verified,
                    user_turn,
                    thinking,
                    text,
                    ex_usage,
                    ex_elapsed,
                    used_model,
                    mark,
                )
            )

        except Exception as e:
            print(f"  [extract]   ERROR: {type(e).__name__}: {e}", flush=True)
            if not dry_run:
                _insert_mark(turn_id_val, f"ERROR: {e}"[:200], "day_extract", is_final=False)
            failed += 1
            _failures.append(
                {
                    "turn_id": turn_id_val,
                    "reason": f"ERROR: {e}",
                    "preview": (text or user_turn or "")[:100],
                }
            )

    # ── Phase 2b: Parallel LLM NLI verify ──────────────────────
    def _nli_worker(task):
        tid, verified, user_turn, thinking, text = task[0], task[1], task[2], task[3], task[4]
        try:
            return (tid, _llm_nli_verify(verified, user_turn, thinking, text))
        except Exception as e:
            print(f"  [extract]   NLI error for {tid[:8]}: {e}", flush=True)
            return (tid, None)

    nli_verified = {}
    if nli_tasks:
        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            futures = [pool.submit(_nli_worker, t) for t in nli_tasks]
            for f in as_completed(futures):
                tid, result = f.result()
                nli_verified[tid] = result

    # ── Phase 2c: Classification + reranker ─────────────────────
    extractions_by_turn = {}
    store_tasks = []
    store_idx = 0

    for task in nli_tasks:
        store_idx += 1
        turn_id_val = task[0]
        verified = nli_verified.get(task[0])
        if verified is None:
            verified = task[1]
        user_turn = task[2]
        thinking = task[3]
        text = task[4]
        ex_usage = task[5]
        ex_elapsed = task[6]
        used_model = task[7]
        mark = task[8]

        try:
            entail = []
            neutral = []
            for v in verified:
                nli = v.get("nli_llm", "NEUTRAL")
                if nli == "ENTAILMENT":
                    v.update(
                        {
                            "faithful": True,
                            "faithful_score": 100,
                            "faithful_method": "nli",
                            "grounding": "GROUNDED",
                        }
                    )
                    entail.append(v)
                elif nli == "CONTRADICTION":
                    v.update(
                        {
                            "faithful": False,
                            "faithful_score": 0,
                            "faithful_method": "nli",
                            "grounding": "UNGROUNDED",
                        }
                    )
                else:
                    neutral.append(v)
            rerankered = _verify_extractions(neutral, user_turn, thinking, text) if neutral else []
            extractions = entail + rerankered

            if not extractions:
                print("  [extract]   No faithful extractions — marking failure")
                if not dry_run:
                    _insert_mark(turn_id_val, mark or "추출 2회실패", used_model, is_final=True)
                    psql_ok(
                        f"UPDATE turns SET pipeline_state = 'extracted' WHERE id = '{esc_sql(turn_id_val)}'::uuid"
                    )
                failed += 1
                _failures.append(
                    {
                        "turn_id": turn_id_val,
                        "reason": mark or "추출 2회실패",
                        "preview": (text or user_turn or "")[:100],
                    }
                )
                continue

            extractions_by_turn[turn_id_val] = extractions
            store_tasks.append(
                (
                    turn_id_val,
                    extractions,
                    user_turn,
                    thinking,
                    text,
                    ex_usage,
                    ex_elapsed,
                    used_model,
                    mark,
                )
            )

        except Exception as e:
            print(f"  [extract]   ERROR: {type(e).__name__}: {e}", flush=True)
            if not dry_run:
                _insert_mark(turn_id_val, f"ERROR: {e}"[:200], "day_extract", is_final=False)
            failed += 1
            _failures.append(
                {
                    "turn_id": turn_id_val,
                    "reason": f"ERROR: {e}",
                    "preview": (text or user_turn or "")[:100],
                }
            )

    # ── Phase 2c-2: Parallel refine ─────────────────────────────
    if extractions_by_turn:
        _refine_batch(extractions_by_turn)

    # ── Phase 2c-3: Store (sequential, DB writes) ──────────────
    for (
        tid,
        extractions,
        user_turn,
        thinking,
        text,
        ex_usage,
        ex_elapsed,
        used_model,
        mark,
    ) in store_tasks:
        extractions = extractions_by_turn.get(tid, extractions)

        if dry_run:
            print(f"  [extract]   [DRY] Would store {len(extractions)} facts")
            total_facts += len(extractions)
            processed += 1
            continue

        try:
            fi = 0
            pt = ex_usage.get("prompt_tokens") if ex_usage else None
            gt = ex_usage.get("completion_tokens") if ex_usage else None
            em = ex_elapsed

            for ex in extractions:
                ft = ex.get("fact_type", "text")
                evidence = ex.get("evidence", "")
                _insert_fact(
                    tid,
                    fi,
                    ft,
                    evidence,
                    used_model,
                    prompt_tokens=pt,
                    gen_tokens=gt,
                    elapsed_ms=em,
                    faithful_score=ex.get("faithful_score"),
                    faithful_method=ex.get("faithful_method"),
                    grounding=ex.get("grounding"),
                    nli_llm=ex.get("nli_llm"),
                    corrected_evidence=ex.get("corrected_evidence"),
                    subject=ex.get("subject"),
                    predicate=ex.get("predicate"),
                    object_=ex.get("object"),
                    qualifiers=ex.get("qualifiers"),
                )
                fi += 1

            if mark:
                _insert_mark(tid, mark, used_model, is_final=False)

            print(f"  [extract]   Stored {fi} facts", flush=True)
            if not dry_run:
                psql_ok(
                    f"UPDATE turns SET pipeline_state = 'extracted' WHERE id = '{esc_sql(tid)}'::uuid"
                )
                _delete_checkpoint(tid)
            heartbeat("day_extract", f"turn {tid[:8]} stored {fi} facts")
            total_facts += fi
            processed += 1

            if _SIGTERM_RECEIVED.is_set():
                print(f"  [extract]   SIGTERM — partial save ({store_idx} turns)", flush=True)
                break

        except Exception as e:
            print(f"  [extract]   ERROR: {type(e).__name__}: {e}", flush=True)
            if not dry_run:
                _insert_mark(tid, f"ERROR: {e}"[:200], "day_extract", is_final=False)
            failed += 1
            _failures.append(
                {
                    "turn_id": tid,
                    "reason": f"ERROR: {e}",
                    "preview": (text or user_turn or "")[:100],
                }
            )

    elapsed = round(time.monotonic() - t_start, 1)

    if not dry_run and (_failures or _noise):
        report = {"failures": _failures, "noise": _noise}
        report_path = "/var/tmp/extract_fail_report.json"
        try:
            with open(report_path, "w") as f:
                f.write(json.dumps(report, ensure_ascii=False))
        except Exception:
            pass

    print(f"\n{'=' * 60}", flush=True)
    noise_part = f", {skipped_noise} noise skip" if skipped_noise else ""
    print(
        f"Done: {processed} processed, {failed} failed{noise_part}, "
        f"{total_facts} facts ({elapsed}s)"
    )
    if dry_run:
        print("  [DRY RUN] No data was written")
    print(f"{'=' * 60}")

    if not dry_run:
        psql_ok(
            "DELETE FROM pipeline_checkpoints cp "
            "WHERE pipeline = 'extract' AND EXISTS ("
            "  SELECT 1 FROM review_facts rf "
            "  WHERE rf.turn_id = cp.turn_id AND rf.source = 'extract_pipeline')"
        )

    return {
        "processed": processed,
        "failed": failed,
        "skipped_noise": skipped_noise,
        "facts": total_facts,
        "elapsed_s": elapsed,
        "ok": processed > 0,
    }


# ── File description phase ────────────────────────────────────────────

_TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".log",
    ".html",
    ".css",
    ".js",
    ".sh",
    ".toml",
    ".xml",
    ".cfg",
    ".ini",
    ".conf",
    ".env",
    ".rst",
    ".tex",
}


def _sample_content(path: str, max_bytes: int = 2048) -> str:
    ext = Path(path).suffix.lower()
    if ext not in _TEXT_EXTENSIONS:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def describe_file_batch(dry_run: bool = False, limit: int = 20) -> Dict[str, Any]:
    from lib.file_registry import scan_undescribed, update_metadata

    files = scan_undescribed()
    if isinstance(files, list) and len(files) > limit:
        files = files[:limit]

    if not files:
        print("[describe-files] No undescribed files found")
        return {"processed": 0, "failed": 0, "ok": True}

    print(f"[describe-files] Describing {len(files)} file(s)")
    processed = 0
    failed = 0

    for f in files:
        fname = f.get("filename", "?")
        mime = f.get("mime_type", "?")
        fsize = f.get("size", 0)
        fsrc = f.get("source", "?")
        content = _sample_content(f.get("path", ""))
        print(f"  [{processed + 1}/{len(files)}] {fname} ({mime}, {fsize}b)")

        parts = [
            f"filename: {fname}",
            f"mime_type: {mime}",
            f"size: {fsize} bytes",
            f"source: {fsrc}",
        ]
        if content:
            parts.append("")
            parts.append("=== content (first 2KB) ===")
            parts.append(content)

        meta = call_llm(
            [
                {"role": "system", "content": SYSTEM_DESCRIBE_FILE},
                {"role": "user", "content": "\n".join(parts)},
            ],
            model="day_extract",
            max_tokens=256,
            temperature=0.1,
            timeout=60,
            json_mode=True,
            return_meta=True,
        )
        raw = meta["content"]
        parsed = _parse_json(raw, "describe_file")
        if not parsed:
            print("    Parse failure, skipping")
            failed += 1
            continue

        desc = parsed.get("description", "")
        tags = parsed.get("tags", [])
        if not desc:
            print("    Empty description from LLM")
            failed += 1
            continue

        print(f"    → {desc}")
        if tags:
            print(f"    tags: {', '.join(tags)}")
        if not dry_run:
            update_metadata(f["id"], description=desc, tags=tags)
        processed += 1

    print(f"[describe-files] Done: {processed} described, {failed} failed")
    return {"processed": processed, "failed": failed, "ok": processed > 0}


# ── CLI ────────────────────────────────────────────────────────────────


def main() -> None:
    signal.signal(signal.SIGTERM, _sigterm_handler)
    _ensure_model_pod("day-extractor", skip_if_healthy=True)
    preflight_checks("extract.py")
    import argparse

    parser = argparse.ArgumentParser(
        description="Extract Pipeline — day_extract → Python verify → store"
    )
    parser.add_argument("--turn-id", help="Process a specific turn UUID")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pulse-context", help="Inject Watchdog Pulse context")
    parser.add_argument(
        "--describe-files",
        action="store_true",
        help="Scan file_registry for undescribed files and generate descriptions",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=None,
        help="Override parallel workers (default: PARALLEL constant)",
    )
    args = parser.parse_args()

    if args.parallel is not None:
        global PARALLEL
        PARALLEL = args.parallel
        print(f"  [extract] PARALLEL overridden to {PARALLEL}", flush=True)

    if args.describe_files:
        result = describe_file_batch(dry_run=args.dry_run, limit=args.limit)
    else:
        result = extract_pipeline(
            turn_id=args.turn_id,
            limit=args.limit,
            dry_run=args.dry_run,
            pulse_context=args.pulse_context,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    from lib.llm_client import recall_tiny

    recall_tiny()


# ── Backward-compatible re-exports ─────────────────────────────────────
# Test files import these from extract.py; re-export from submodules
from extract_llm import (  # noqa: E402, F401
    SYSTEM_DAY_EXTRACT,
)
from extract_verify import (  # noqa: E402, F401
    _check_faithfulness,
    _cosine_faithfulness,
)

COSINE_FAITHFUL = 0.75  # noqa: E402 — used by test files
COSINE_UNFAITHFUL = 0.40  # noqa: E402


if __name__ == "__main__":
    main()
