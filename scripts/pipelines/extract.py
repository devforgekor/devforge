#!/usr/bin/env python3
# Status: production
# Path: 15m_cycle.sh
"""Extract Pipeline - checkpoint-based perpetual fact extraction.

SSOT: turns.created_at. Checkpoint in pipeline_checkpoint(phase=extract).
Each cycle: SELECT WHERE created_at > checkpoint -extract -advance.
Failed turns do NOT advance checkpoint -next cycle retries automatically.
DB UNIQUE (turn_id, fact_index, extract_model) prevents duplicate storage.

Flow:
  Phase 1: SELECT unprocessed (created_at > checkpoint, limit 50)
  Phase 2: day_extract extraction (user/thinking/text)
  Phase 3: Python diff verify (faithfulness check)
  Phase 4: Failure handling - retry day_extract or fallback to day_mcp with marking
  Phase 5: day_mcp MCP fields (tldr, intent, entities, tags)
  Phase 6: Store to review_facts + enqueue + advance checkpoint

Usage:
  python3 scripts/pipelines/extract.py                          # process from checkpoint
  python3 scripts/pipelines/extract.py --turn-id <uuid>            # single turn (debug)
  python3 scripts/pipelines/extract.py --limit 50                  # batch cap
  python3 scripts/pipelines/extract.py --dry-run                   # simulate, no writes
"""

import json
import os
import re
import sys
import subprocess as sp
import time
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.llm_client import call_llm
from lib.llm.json_parser import save_dlq, parse_llm_json
from lib.queue_writer import enqueue_review

# ── Constants ──────────────────────────────────────────────────────────────
# Timeout/token/temp for extraction (day_extract) vs MCP fields generation (day_mcp)
TIMEOUT_EXTRACT = 180
TIMEOUT_MCP = 300
MAX_TOKENS_EXTRACT = 2048
MAX_TOKENS_MCP = 2048
TEMP_EXTRACT = 0.1
TEMP_MCP = 0.1
BATCH_LIMIT = 100

# ── System prompts ─────────────────────────────────────────────────────────
SYSTEM_DAY_EXTRACT = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN extractions on:
- Faithfulness (0-10): Is every extraction directly traceable to the source?
- Precision (0-10): Are extractions factual statements, not interpretations?
- Recall (0-10): Are all key facts captured (up to 5 per type)?
- Conciseness (0-10): Is evidence brief and to the point?

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ],
  "rubric_evaluation": {
    "faithfulness": "0-10",
    "faithfulness_justification": "...",
    "precision": "0-10",
    "precision_justification": "...",
    "recall": "0-10",
    "recall_justification": "...",
    "conciseness": "0-10",
    "conciseness_justification": "..."
  }
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

SYSTEM_DAY_MCP = """\
You are a conversation analyst preparing structured metadata for an MCP
(Model Context Protocol) system. Given the original conversation turn and
the extracted facts, produce structured MCP fields.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN MCP output on:
- Accuracy (0-10): Are fields directly derivable from turn content?
- Completeness (0-10): Are all relevant entities captured without hallucination?
- Conciseness (0-10): Is tldr brief and tags minimal but useful?

Output STRICT JSON:
{
  "tldr": "One-line summary (max 15 words) — what this turn is about",
  "intent": "question|request|report|clarification|code_change|debug|design|other",
  "entities": {
    "files": ["relative/file/path.py"],
    "technologies": ["Python", "FastAPI", ...],
    "functions": ["function_name"],
    "mentioned_users": []
  },
  "tags": ["tag1", "tag2"],
  "rubric_evaluation": {
    "accuracy": "0-10",
    "accuracy_justification": "...",
    "completeness": "0-10",
    "completeness_justification": "...",
    "conciseness": "0-10",
    "conciseness_justification": "..."
  }
}

Rules:
- tldr must be factual and directly derivable from the turn content
- intent must be one of the enumerated values
- entities.files: only include file paths explicitly mentioned in the turn
- entities.technologies: programming languages, frameworks, tools mentioned
- entities.functions: function/class/method names mentioned
- tags: 2-5 keywords for discovery and routing
- If a field has no relevant data, use an empty array []"""

SYSTEM_FALLBACK = """\
You are a fact extraction specialist handling a difficult turn. The initial extractor
failed twice to extract faithful facts from this turn — previous extractions
contained hallucinated content not present in the source. Be EXTRA cautious:

1. Verify every extracted fact exists verbatim in the source.
2. When in doubt, OMIT the fact rather than include it.
3. Prefer under-extraction over hallucination.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN fallback extraction on:
- Caution (0-10): Are extractions conservative — omitted when unsure?
- Faithfulness (0-10): Is every extraction verifiable in source?
- Usefulness (0-10): Does this provide value above initial extraction failures?

Output STRICT JSON:
{
  "fallback_note": "Why the initial extraction may have struggled (1 sentence)",
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ],
  "rubric_evaluation": {
    "caution": "0-10",
    "caution_justification": "...",
    "faithfulness": "0-10",
    "faithfulness_justification": "...",
    "usefulness": "0-10",
    "usefulness_justification": "..."
  }
}"""


# ── JSON parser ────────────────────────────────────────────────────────────
_THINK_RE = re.compile(r"<think[^>]*>.*?</think>", re.DOTALL)


def _parse_json(raw: str, label: str = "LLM", attempt: int = 1) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM output using shared parse_llm_json + DLQ.

    Strips <think> blocks before parsing (R1 reasoning), falls through to
    parse_llm_json (stdlib → json_repair). Saves parse failures to DLQ.
    """
    cleaned = _THINK_RE.sub("", raw).strip()
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(raw, stage=f"extract_{label}", error="parse_llm_json returned None",
                 attempt=attempt)
    return result


# ── Hallucination check ───────────────────────────────────────────────────
def _check_faithfulness(evidence: str, source: str) -> bool:
    """Return True if evidence is a substring of source (faithful)."""
    if not evidence or not source:
        return False
    ev = " ".join(evidence.split())
    src = " ".join(source.split())
    return ev.lower() in src.lower()


def _verify_entities(mcp_data: Optional[Dict],
                     project_root: str = "/opt/projects/server") -> Dict:
    """Verify entities.files exist and entities.functions can be found.

    Returns dict with 'files' and 'symbols' verification results.
    """
    entities = mcp_data.get("entities", {}) if mcp_data else {}
    if not isinstance(entities, dict):
        entities = {}
    verified: Dict[str, list] = {"files": [], "symbols": []}

    for filepath in entities.get("files", []):
        full = os.path.join(project_root, filepath)
        exists = os.path.exists(full)
        verified["files"].append({"path": filepath, "exists": exists})

    for sym in entities.get("functions", []):
        found = _find_symbol(sym, project_root)
        verified["symbols"].append({"name": sym, "found": found})

    return verified


def _find_symbol(symbol: str, project_root: str) -> bool:
    """Search for a Python function/class definition using grep."""
    try:
        r = sp.run(
            ["grep", "-Erq", f"^(def |class |async def ){re.escape(symbol)}[( ]",
             "--include=*.py", project_root],
            capture_output=True, timeout=15,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── Phase 2: extraction ────────────────────────────────────────────────
def _extract_facts(user_turn: str, thinking: str, text: str,
                   attempt: int = 1,
                   prev_unfaithful: Optional[List[str]] = None
                   ) -> Optional[Dict[str, Any]]:
    """Run day_extract extraction. Returns {extractions, usage, timings, elapsed_ms}."""
    parts = [
        "=== user_turn ===",
        user_turn or "(empty)",
        "",
        "=== thinking ===",
        thinking or "(empty)",
        "",
        "=== text ===",
        text or "(empty)",
    ]
    if attempt > 1 and prev_unfaithful:
        parts.append("")
        parts.append("Previous attempt produced UNFAITHFUL extractions (not in source):")
        for i, ev in enumerate(prev_unfaithful, 1):
            parts.append(f"  {i}. {ev[:200]}")
        parts.append("Do NOT repeat these. Only extract what is EXPLICITLY present.")
    elif attempt > 1:
        parts.append("")
        parts.append("Note: Retry. Previous attempt had unfaithful extractions.")

    meta = call_llm(
        [{"role": "system", "content": SYSTEM_DAY_EXTRACT},
         {"role": "user", "content": "\n".join(parts)}],
        model="day_extract",
        max_tokens=MAX_TOKENS_EXTRACT, temperature=TEMP_EXTRACT, timeout=TIMEOUT_EXTRACT,
        json_mode=True, return_meta=True,
    )
    raw = meta["content"]
    parsed = _parse_json(raw, "day_extract", attempt=attempt)
    if parsed is None:
        return None
    ex = parsed.get("extractions", [])
    result = {"extractions": ex, "usage": meta["usage"], "timings": meta["timings"],
              "elapsed_ms": meta["elapsed_ms"]} if isinstance(ex, list) else None
    if result and "rubric_evaluation" in parsed:
        result["rubric_evaluation"] = parsed["rubric_evaluation"]
    return result


# ── Phase 3: Python diff verify ────────────────────────────────────────────
def _verify_extractions(
    extractions: List[Dict[str, Any]],
    user_turn: str, thinking: str, text: str,
) -> List[Dict[str, Any]]:
    """Check each extraction is faithful. Returns [{faithful, ...}]."""
    source_map = {"user": user_turn, "thinking": thinking, "text": text}
    results = []
    for ex in extractions:
        ft = ex.get("fact_type", "")
        evidence = ex.get("evidence", "")
        faithful = _check_faithfulness(evidence, source_map.get(ft, ""))
        results.append({
            "fact_type": ft,
            "evidence": evidence,
            "category": ex.get("category", "other"),
            "faithful": faithful,
        })
    return results


# ── Phase 5: day_mcp MCP fields generation (default: day_mcp, --mcp-model) ─────
def _generate_mcp_fields(user_turn: str, thinking: str, text: str,
                         model: str,
                         extractions: Optional[List[Dict]] = None
                         ) -> Optional[Dict[str, Any]]:
    """Generate MCP metadata fields (tldr, intent, entities, tags) via *model* (default: day_mcp).

    Returns dict with MCP fields + usage/timings metadata.
    """
    parts = [
        "=== user_turn ===", user_turn or "(empty)",
        "", "=== thinking ===", thinking or "(empty)",
        "", "=== text ===", text or "(empty)",
    ]
    if extractions:
        parts.append("")
        parts.append("=== extracted facts ===")
        for ex in extractions:
            parts.append(f"  [{ex.get('fact_type','?')}] {ex.get('evidence','')[:300]}")
    meta = call_llm(
        [{"role": "system", "content": SYSTEM_DAY_MCP},
         {"role": "user", "content": "\n".join(parts)}],
        model=model,
        max_tokens=MAX_TOKENS_MCP, temperature=TEMP_MCP, timeout=TIMEOUT_MCP,
        json_mode=True, return_meta=True,
    )
    result = _parse_json(meta["content"], "MCP fields")
    if result:
        result["_meta"] = {"usage": meta["usage"], "timings": meta["timings"],
                           "elapsed_ms": meta["elapsed_ms"], "model": model}
    return result


# ── Phase 4: Fallback extraction (after day_extract double-failure) ────────────
def _fallback_extract(user_turn: str, thinking: str, text: str,
                      model: str = "day_mcp") -> Optional[Dict[str, Any]]:
    """Fallback extraction after 3B double-failure. Uses *model* (default day_mcp)."""
    parts = [
        "=== user_turn ===", user_turn or "(empty)",
        "", "=== thinking ===", thinking or "(empty)",
        "", "=== text ===", text or "(empty)",
    ]
    meta = call_llm(
        [{"role": "system", "content": SYSTEM_FALLBACK},
         {"role": "user", "content": "\n".join(parts)}],
        model=model,
        max_tokens=MAX_TOKENS_MCP, temperature=TEMP_MCP, timeout=TIMEOUT_MCP,
        json_mode=True, return_meta=True,
    )
    raw = meta["content"]
    parsed = _parse_json(raw, f"{model} fallback")
    if parsed is None:
        return None
    ex = parsed.get("extractions", [])
    if not isinstance(ex, list):
        return None
    return {"fallback_note": parsed.get("fallback_note", ""), "extractions": ex,
            "usage": meta["usage"], "timings": meta["timings"], "elapsed_ms": meta["elapsed_ms"],
            "rubric_evaluation": parsed.get("rubric_evaluation", {})}


# ── DB writers ─────────────────────────────────────────────────────────────
def _insert_fact(turn_id: str, fact_index: int, fact_type: str,
                 evidence: str, extract_model: str,
                 prompt_tokens: Optional[int] = None,
                 gen_tokens: Optional[int] = None,
                 elapsed_ms: Optional[float] = None) -> bool:
    """Insert a fact row into review_facts with optional timing metadata."""
    cols = ["turn_id", "fact_index", "fact_type", "evidence", "extract_model", "verdict",
            "source", "fact_action", "fact_confidence"]
    vals = [f"'{esc_sql(turn_id)}'::uuid", str(fact_index),
            f"'{esc_sql(fact_type)}'", f"'{esc_sql(evidence[:5000])}'",
            f"'{esc_sql(extract_model)}'", "'pending'",
            "'extract_pipeline'", "'store'", "100"]
    if prompt_tokens is not None:
        cols.append("prompt_tokens")
        vals.append(str(prompt_tokens))
    if gen_tokens is not None:
        cols.append("gen_tokens")
        vals.append(str(gen_tokens))
    if elapsed_ms is not None:
        cols.append("elapsed_ms")
        vals.append(f"{elapsed_ms:.1f}")
    set_clauses = []
    if prompt_tokens is not None:
        set_clauses.append(f"prompt_tokens = {prompt_tokens}")
    if gen_tokens is not None:
        set_clauses.append(f"gen_tokens = {gen_tokens}")
    if elapsed_ms is not None:
        set_clauses.append(f"elapsed_ms = {elapsed_ms:.1f}")

    sql = (
        f"INSERT INTO review_facts ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)}) "
        f"ON CONFLICT (turn_id, fact_index, extract_model) "
        f"DO UPDATE SET evidence = EXCLUDED.evidence"
        + (f", {', '.join(set_clauses)}" if set_clauses else "")
    )
    return psql_ok(sql)


def _insert_mark(turn_id: str, mark: str, extract_model: str, is_final: bool = False) -> bool:
    """Insert a failure-marker fact row (for traceability).

    is_final=True: source='extract_pipeline' — blocks reprocessing.
    is_final=False: source='extract_marker' — allows retry on next batch.
    """
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


# ── Checkpoint ────────────────────────────────────────────────────────────────
def _get_checkpoint() -> str:
    """Return max_created_at from pipeline_checkpoint for extract phase."""
    return psql("SELECT max_created_at::text FROM pipeline_checkpoint WHERE phase = 'extract'") or '-infinity'


def _advance_checkpoint(created_at_str: str):
    """Advance checkpoint to created_at if newer. SSOT: turns.created_at."""
    psql_ok(
        f"UPDATE pipeline_checkpoint "
        f"SET max_created_at = '{esc_sql(created_at_str)}'::timestamptz, "
        f"    updated_at = NOW() "
        f"WHERE phase = 'extract' "
        f"  AND max_created_at < '{esc_sql(created_at_str)}'::timestamptz"
    )


# ── Phase 1: Select turns ─────────────────────────────────────────────────
def _get_unprocessed_turns(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Return turns created after checkpoint, ordered by creation time.

    Checkpoint = last successfully processed turn's created_at.
    Failed turns do NOT advance checkpoint → retried on next cycle.
    """
    checkpoint = _get_checkpoint()
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, "
        "  t.source_message_id, t.created_at, "
        "  t.conversation_id, t.seq "
        "FROM turns t "
        "WHERE t.text != '' "
        f"  AND t.created_at > '{esc_sql(checkpoint)}'::timestamptz "
        "ORDER BY t.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    turns = []
    for row in rows:
        turns.append({
            "id": row.get("id", ""),
            "user_turn": row.get("user_turn", ""),
            "thinking": row.get("thinking") or None,
            "text": row.get("text", ""),
            "source_message_id": row.get("source_message_id", ""),
            "created_at": row.get("created_at", ""),
            "conversation_id": row.get("conversation_id", ""),
            "seq": row.get("seq", 0) or 0,
        })
    return turns


# ── Main pipeline ──────────────────────────────────────────────────────────
def extract_pipeline(
    turn_id: Optional[str] = None,
    limit: int = BATCH_LIMIT,
    dry_run: bool = False,
    mcp_model: str = "day_mcp",
) -> Dict[str, Any]:
    """Run day_extract extractive → Python verify → day_mcp MCP fields per turn."""
    t_start = time.monotonic()

    print(f"\n{'=' * 60}")
    print(f"Extract Pipeline — day_extract → Python verify → {mcp_model} MCP fields")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    # ── Phase 1: Select turns ──────────────────────────────────────────
    if turn_id:
        sql = (
            "SELECT t.id, t.user_turn, t.thinking, t.text, "
            "  t.source_message_id, t.created_at, "
            "  t.conversation_id, t.seq "
            f"FROM turns t WHERE t.id = '{esc_sql(turn_id)}'::uuid"
        )
        rows = psql_json(sql)
        if not rows:
            print(f"[extract] Turn not found: {turn_id}")
            return {"processed": 0, "failed": 1, "facts": 0, "ok": False}
        r = rows[0]
        turns = [{
            "id": r["id"], "user_turn": r["user_turn"],
            "thinking": r.get("thinking") or None, "text": r["text"],
            "source_message_id": r.get("source_message_id", ""),
            "created_at": r["created_at"],
            "conversation_id": r["conversation_id"],
            "seq": r.get("seq", 0) or 0,
        }]
    else:
        turns = _get_unprocessed_turns(limit)

    if not turns:
        print("[extract] No unprocessed turns found")
        return {"processed": 0, "failed": 0, "facts": 0, "ok": True}

    print(f"[extract] Processing {len(turns)} turn(s)")
    total_facts = 0
    processed = 0
    failed = 0
    _prev_bad: Dict[int, List[str]] = {}

    for idx, turn in enumerate(turns, 1):
        tid = turn["id"]
        ut = turn["user_turn"] or ""
        th = turn["thinking"] or ""
        tx = turn["text"] or ""
        print(f"\n[{idx}/{len(turns)}] Turn {tid[:8]}... "
              f"user={len(ut)}ch think={len(th)}ch text={len(tx)}ch")

        try:
            # ── Phase 2–4: extraction with retry/fallback ──────────
            extractions: Optional[List[Dict[str, Any]]] = None
            mark = ""
            used_model = "day_extract"

            for attempt in (1, 2):
                print(f"  [extract] day_extract attempt {attempt}...")
                prev_unfaithful = _prev_bad.get(str(attempt - 1)) if attempt > 1 else None
                ex_result = _extract_facts(ut, th, tx, attempt=attempt,
                                        prev_unfaithful=prev_unfaithful)
                if ex_result is None:
                    print(f"  [extract]   Parse failure")
                    mark = "추출 1차 실패" if attempt == 1 else "추출 2회실패 → fallback"
                    continue

                raw_ex = ex_result["extractions"]
                ex_usage = ex_result.get("usage", {})
                ex_timings = ex_result.get("timings", {})
                ex_elapsed = ex_result.get("elapsed_ms", 0)
                verified = _verify_extractions(raw_ex, ut, th, tx)
                faithful = [v for v in verified if v["faithful"]]
                unfaithful = [v for v in verified if not v["faithful"]]
                _prev_bad[attempt] = [v["evidence"] for v in unfaithful]
                print(f"  [extract]   {len(faithful)} faithful, "
                      f"{len(unfaithful)} unfaithful")

                if not unfaithful:
                    extractions = faithful
                    break

                # Unfaithful extractions this attempt
                if attempt == 1:
                    mark = "추출 1차 실패"
                    print(f"  [extract]   → {mark}")
                else:
                    mark = "추출 2회실패 → fallback"
                    print(f"  [extract]   → {mark}")

            # After both extraction attempts: if still failing, try mcp_model fallback
            if extractions is None:
                used_model = mcp_model  # falls back to same model defined by --mcp-model
                print(f"  [extract] {used_model} fallback extraction...")
                fallback = _fallback_extract(ut, th, tx, model=used_model)
                if fallback and fallback.get("extractions"):
                    f_ex = fallback["extractions"]
                    f_ver = _verify_extractions(f_ex, ut, th, tx)
                    faithful = [v for v in f_ver if v["faithful"]]
                    if faithful:
                        extractions = faithful
                        print(f"  [extract]   {used_model}: {len(faithful)} faithful facts")
                    else:
                        print(f"  [extract]   {used_model} fallback also unfaithful")
                else:
                    print(f"  [extract]   {used_model} fallback also empty")

            if extractions is None:
                print(f"  [extract]   No faithful extractions — marking failure")
                if not dry_run:
                    _insert_mark(tid, mark or "추출 2회실패", used_model,
                                 is_final=True)
                failed += 1
                continue

            # ── Phase 5: MCP fields generation ──────────────────────────
            print(f"  [extract] {mcp_model} MCP fields...")
            mcp_result = _generate_mcp_fields(ut, th, tx,
                                              model=mcp_model,
                                              extractions=extractions)
            mcp_tldr = mcp_result.get("tldr", "") if mcp_result else ""
            mcp_intent = mcp_result.get("intent", "other") if mcp_result else "other"
            mcp_entities = mcp_result.get("entities", {}) if mcp_result else {}
            mcp_tags = mcp_result.get("tags", []) if mcp_result else []
            if mcp_tldr:
                print(f"  [extract]   tldr: {mcp_tldr}")
            if mcp_intent:
                print(f"  [extract]   intent: {mcp_intent}")
            if mcp_entities:
                print(f"  [extract]   entities: files={len(mcp_entities.get('files',[]))}, "
                      f"funcs={len(mcp_entities.get('functions',[]))}")

            # ── Entity verification ──────────────────────────────────────
            if mcp_result and mcp_result.get("entities"):
                verified = _verify_entities(mcp_result)
                mcp_result["verified"] = verified
                n_files = len(verified.get("files", []))
                n_syms = len(verified.get("symbols", []))
                n_missing_files = sum(1 for f in verified.get("files", []) if not f["exists"])
                n_missing_syms = sum(1 for s in verified.get("symbols", []) if not s["found"])
                print(f"  [extract]   verified: {n_files} files ({n_missing_files} missing), "
                      f"{n_syms} symbols ({n_missing_syms} missing)")

            # ── Phase 6: Store ───────────────────────────────────────
            if dry_run:
                print(f"  [extract]   [DRY] Would store {len(extractions)} facts + MCP fields")
                total_facts += len(extractions)
                if mcp_tldr:
                    total_facts += 1
                processed += 1
                continue

            fi = 0
            # Extract timing for the successful attempt
            pt = ex_usage.get("prompt_tokens") if ex_usage else None
            gt = ex_usage.get("completion_tokens") if ex_usage else None
            em = ex_elapsed

            for ex in extractions:
                ft = ex.get("fact_type", "text")
                evidence = ex.get("evidence", "")
                _insert_fact(tid, fi, ft, evidence, used_model,
                             prompt_tokens=pt, gen_tokens=gt, elapsed_ms=em)
                fi += 1

            mcp_meta = mcp_result.get("_meta", {}) if mcp_result else {}
            mcp_prompt_tokens = mcp_meta.get("usage", {}).get("prompt_tokens") if mcp_meta else None
            mcp_gen_tokens = mcp_meta.get("usage", {}).get("completion_tokens") if mcp_meta else None
            mcp_elapsed_ms = mcp_meta.get("elapsed_ms") if mcp_meta else None

            # Strip _meta from stored MCP (for search/analysis), keep in activity_log
            mcp_for_storage = {k: v for k, v in mcp_result.items() if k != "_meta"} if mcp_result else None

            if mcp_result:
                _insert_fact(tid, fi, "mcp_meta", json.dumps(mcp_for_storage, ensure_ascii=False),
                             mcp_model, prompt_tokens=mcp_prompt_tokens, gen_tokens=mcp_gen_tokens, elapsed_ms=mcp_elapsed_ms)
                fi += 1

            # Write the failure marker if any (only on successful extraction)
            if mark:
                _insert_mark(tid, mark, used_model, is_final=False)

            print(f"  [extract]   Stored {fi} facts")
            total_facts += fi
            processed += 1

            # Enqueue for downstream P→R→J review → verify (nightly)
            enqueue_review(
                entry_type="extract_result",
                source="extract_pipeline.py",
                title=f"Extract: {tid[:8]} ({fi} facts)",
                summary=f"{fi} facts ({used_model}) — {mcp_tldr}" if mcp_tldr else (
                    f"{fi} facts ({used_model})" + (f" — {mark}" if mark else "")),
                body={
                    "turn_id": tid,
                    "fact_count": fi,
                    "faithful_count": len(extractions),
                    "extract_model": used_model,
                    "extract_prompt_tokens": pt,
                    "extract_completion_tokens": gt,
                    "extract_elapsed_ms": em,
                    "mcp_prompt_tokens": mcp_prompt_tokens,
                    "mcp_completion_tokens": mcp_gen_tokens,
                    "mcp_elapsed_ms": mcp_elapsed_ms,
                    "mark": mark,
                    "mcp": mcp_result,
                },
                model=mcp_model,
                turn_ids=[tid],
                tags=["extract", "fact_extraction"],
                queue_status="pending",
            )

            # Advance checkpoint to this turn's created_at
            _advance_checkpoint(turn["created_at"])

        except Exception as e:
            print(f"  [extract]   ERROR: {e}")
            if not dry_run:
                _insert_mark(tid, f"ERROR: {e}"[:200], "day_extract", is_final=False)
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}")
    print(f"Done: {processed} processed, {failed} failed, "
          f"{total_facts} facts ({elapsed}s)")
    if dry_run:
        print("  [DRY RUN] No data was written")
    print(f"{'=' * 60}")

    return {"processed": processed, "failed": failed,
            "facts": total_facts, "elapsed_s": elapsed, "ok": failed == 0}


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description=f"Extract Pipeline — day_extract → Python → day_mcp MCP (default)")
    parser.add_argument("--turn-id", help="Process a specific turn UUID")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--mcp-model", default="day_mcp",
                        help=f"Model for MCP fields generation (default: day_mcp)")
    args = parser.parse_args()

    result = extract_pipeline(
        turn_id=args.turn_id,
        limit=args.limit,
        dry_run=args.dry_run,
        mcp_model=args.mcp_model,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
