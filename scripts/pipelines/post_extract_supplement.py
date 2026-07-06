#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.sh — Post-Extract Supplement (offline missing-fact LLM)
"""Post-extract supplement — offline LLM call to find missing facts.

Runs after day_extract sets pipeline_state='extracted', before day_verify.
Calls LLM on 8082 to identify important facts missed by chunk-based extraction.
New facts stored as 'extracted' with source='extract_pipeline' → pass through
verify → enrich → embed pipeline normally (day_verify.py picks them up).

Budget: max 10 turns per run (~180s/turn = ~1800s). Use --limit to cap.
8082 contention: supplement acquires the port from day_extract (still running)
and releases before day_verify starts.

Usage:
  python3 scripts/pipelines/post_extract_supplement.py [--limit 10] [--dry-run]
"""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from extract_llm import _call_with_8082_retry, _parse_json
from lib.db import esc_sql, psql_json, psql_ok

BATCH_LIMIT = 10

_SYSTEM_SUPPLEMENT = """\
You are a fact extraction auditor reviewing existing extractions.

Below is the ORIGINAL TEXT and the FACTS ALREADY EXTRACTED from it.

Your task: Identify important factual statements in the ORIGINAL TEXT that
are NOT captured by the ALREADY EXTRACTED FACTS. Focus on:
- Causal relationships (X caused Y, X led to Y)
- Secondary attributes (values, thresholds, measurements, versions, ports)
- Post-fix outcomes (results, improvements, regressions)
- Contextual details (timeframes, conditions, locations, configurations)

RULES:
1. Only extract facts DIRECTLY stated in the ORIGINAL TEXT.
2. Each fact must be new — not duplicating any ALREADY EXTRACTED FACT.
3. Max 3 supplementary facts. Fewer precise facts > many noisy ones.
4. If no important facts are missing, return empty list.
5. Evidence must be a direct quote ending with a period.
6. Predicate is snake_case action verb phrase (2-5 words).
7. Subject must be a specific entity name (not generic placeholder).

Output ONLY valid JSON. No markdown fences.
{"supplementary_facts": [{"evidence":"...","fact_type":"text","category":"code|decision|explanation|requirement|other","subject":"...","predicate":"snake_case","object":"value","source_context":"..."}]}
Empty: {"supplementary_facts":[]}"""


def _get_turns_with_facts(limit: int = BATCH_LIMIT) -> List[Dict]:
    """Get extracted turns that have extract_pipeline facts."""
    sql = f"""
        SELECT DISTINCT ON (t.id)
               t.id, t.user_turn, t.thinking, t.text, t.detected_lang
        FROM turns t
        JOIN review_facts rf ON rf.turn_id = t.id
        WHERE t.pipeline_state = 'extracted'
          AND rf.source = 'extract_pipeline'
          AND rf.fact_action = 'extracted'
        ORDER BY t.id, t.created_at ASC
        LIMIT {limit}
    """
    rows = psql_json(sql)
    return rows if rows else []


def _get_existing_facts(turn_id: str) -> List[Dict]:
    """Get already-extracted facts for a turn."""
    sql = f"""
        SELECT subject, predicate, object, evidence, fact_type,
               qualifiers, fact_index
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
          AND source = 'extract_pipeline'
          AND fact_action = 'extracted'
        ORDER BY fact_index ASC
    """
    rows = psql_json(sql)
    return rows if rows else []


def _format_facts_for_prompt(facts: List[Dict]) -> str:
    """Format existing facts as concise text for the LLM prompt."""
    if not facts:
        return "(no facts extracted)"
    lines = []
    for i, f in enumerate(facts):
        subj = f.get("subject", "?") or "?"
        pred = f.get("predicate", "?") or "?"
        obj = f.get("object", "?") or "?"
        lines.append(f"{i+1}. ({subj}, {pred}, {obj})")
    return "\n".join(lines)


def _build_source_text(turn: Dict) -> str:
    """Build concatenated source text from turn fields."""
    parts = []
    user = turn.get("user_turn", "") or ""
    text = turn.get("text", "") or ""
    if user:
        parts.append(f"=== USER MESSAGE ===\n{user}")
    if text:
        parts.append(f"=== ASSISTANT RESPONSE ===\n{text}")
    # Include thinking only if it has meaningful content
    thinking = turn.get("thinking", "") or ""
    if thinking and len(thinking) > 20:
        parts.append(f"=== THINKING ===\n{thinking}")
    return "\n\n".join(parts)


def _is_duplicate(turn_id: str, subject: str, predicate: str, object_: str) -> bool:
    """Check if a fact already exists for this turn (any extract_model)."""
    sql = f"""
        SELECT 1 FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
          AND source = 'extract_pipeline'
          AND fact_action = 'extracted'
          AND subject = '{esc_sql(subject)}'
          AND predicate = '{esc_sql(predicate)}'
          AND object = '{esc_sql(object_)}'
        LIMIT 1
    """
    return bool(psql_json(sql))


def _call_supplement_llm(
    source_text: str, existing_facts_formatted: str
) -> Optional[List[Dict]]:
    """Call LLM on 8082 to find missing facts."""
    max_source_chars = 4000
    if len(source_text) > max_source_chars:
        source_text = source_text[:max_source_chars] + "\n...[truncated]"

    user_content = (
        f"ORIGINAL TEXT:\n{source_text}\n\n"
        f"ALREADY EXTRACTED FACTS:\n{existing_facts_formatted}"
    )

    from lib.llm_client import call_llm

    try:
        meta = _call_with_8082_retry(
            call_llm,
            [
                {"role": "system", "content": _SYSTEM_SUPPLEMENT},
                {"role": "user", "content": user_content},
            ],
            model="day_extract",
            max_tokens=512,
            temperature=0.0,
            timeout=180,
            json_mode=True,
            return_meta=True,
        )
    except Exception as e:
        print(f"    [supplement] LLM call failed: {e}", flush=True)
        return None

    raw = meta["content"]
    parsed = _parse_json(raw, "supplement")
    if parsed is None:
        print(f"    [supplement] parse failed", flush=True)
        return None

    new_facts = parsed.get("supplementary_facts", [])
    if not isinstance(new_facts, list):
        new_facts = []
    return new_facts


def _get_next_fact_index(turn_id: str) -> int:
    """Get the next available fact_index for a turn."""
    sql = f"""
        SELECT COALESCE(MAX(fact_index), -1) + 1 AS next_idx
        FROM review_facts
        WHERE turn_id = '{esc_sql(turn_id)}'::uuid
    """
    rows = psql_json(sql)
    if rows:
        return int(rows[0].get("next_idx", 0))
    return 0


def _clean_predicate(pred: str) -> str:
    """Normalize predicate to snake_case."""
    p = pred.strip().lower()
    p = re.sub(r"[\s\-]+", "_", p)
    p = re.sub(r"[^a-z0-9_]", "", p).strip("_")
    return p[:60]


def _store_supplement_fact(
    turn_id: str,
    fact_index: int,
    fact: Dict,
) -> bool:
    """Store a supplement fact as extracted state for normal pipeline processing."""
    evidence = (fact.get("evidence") or "").strip()
    if not evidence:
        return False
    if not evidence.endswith((".", "!", "?")):
        evidence += "."

    fact_type = fact.get("fact_type", "text") or "text"
    category = fact.get("category", "explanation") or "explanation"
    subject = (fact.get("subject") or "").strip()
    predicate = _clean_predicate(fact.get("predicate", ""))
    object_ = (fact.get("object") or "").strip()
    source_context = (fact.get("source_context") or "")[:5000]

    if not subject or not predicate or not object_:
        return False
    if len(predicate) < 3:
        return False

    qualifiers = fact.get("qualifiers") or {}
    qjson = json.dumps(qualifiers).replace("'", "''")

    sql = (
        f"INSERT INTO review_facts "
        f"(turn_id, fact_index, fact_type, evidence, extract_model, verdict, "
        f" source, fact_action, subject, predicate, object, qualifiers, "
        f" corrected_evidence, category) "
        f"VALUES ("
        f"'{esc_sql(turn_id)}'::uuid, "
        f"{fact_index}, "
        f"'{esc_sql(fact_type)}', "
        f"'{esc_sql(evidence[:5000])}', "
        f"'day_supplement', "
        f"'pending', "
        f"'extract_pipeline', "
        f"'extracted', "
        f"'{esc_sql(subject)}', "
        f"'{esc_sql(predicate)}', "
        f"'{esc_sql(object_)}', "
        f"'{qjson}'::jsonb, "
        f"'{esc_sql(source_context)}', "
        f"'{esc_sql(category)}'"
        f") "
        f"ON CONFLICT (turn_id, fact_index, extract_model) DO NOTHING"
    )
    return psql_ok(sql)


def supplement_pipeline(limit: int = BATCH_LIMIT, dry_run: bool = False) -> Dict[str, Any]:
    t_start = time.monotonic()
    print(f"\n{'=' * 60}")
    print("Post-Extract Supplement — LLM missing-fact completion")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    turns = _get_turns_with_facts(limit)
    if not turns:
        print("[supplement] No extracted turns with facts found")
        return {"processed": 0, "total_supplements": 0, "ok": True}

    print(f"[supplement] Found {len(turns)} extracted turn(s) for supplement review")

    total_supplements = 0
    processed = 0
    skipped_dup = 0
    skipped_empty = 0

    for idx, turn in enumerate(turns):
        turn_id = turn["id"]
        print(f"\n[{idx+1}/{len(turns)}] Turn {turn_id[:8]}...")

        source_text = _build_source_text(turn)
        existing_facts = _get_existing_facts(turn_id)

        if not source_text:
            print(f"    [supplement] Empty source — skip")
            continue

        print(f"    Source: {len(source_text)}ch, Existing: {len(existing_facts)} facts")

        existing_formatted = _format_facts_for_prompt(existing_facts)

        print(f"    [supplement] Calling 8082 LLM...", flush=True)
        new_facts = _call_supplement_llm(source_text, existing_formatted)

        if new_facts is None:
            print(f"    [supplement] LLM error — skip")
            continue

        print(f"    [supplement] LLM returned {len(new_facts)} fact(s)")

        stored = 0
        for fact in new_facts:
            subject = (fact.get("subject") or "").strip()
            predicate = _clean_predicate(fact.get("predicate", ""))
            object_ = (fact.get("object") or "").strip()

            if not subject or not predicate or not object_:
                skipped_empty += 1
                continue

            if dry_run:
                stored += 1
                print(f"      [DRY] ({subject}, {predicate}, {object_})")
                continue

            if _is_duplicate(turn_id, subject, predicate, object_):
                skipped_dup += 1
                continue

            fact_index = _get_next_fact_index(turn_id)
            if _store_supplement_fact(turn_id, fact_index, fact):
                stored += 1
                print(f"      fi={fact_index}: ({subject}, {predicate}, {object_})")

        if stored:
            print(f"    [supplement] Stored {stored} new fact(s)")
        else:
            print(f"    [supplement] No new facts stored")
            if skipped_dup:
                print(f"      ({skipped_dup} skipped as duplicates)")

        total_supplements += stored
        processed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}")
    print(
        f"Done: {processed} turns, {total_supplements} supplement facts "
        f"({skipped_dup} dup, {skipped_empty} empty) ({elapsed}s)"
    )
    if dry_run:
        print("  [DRY RUN] No data was written")
    print(f"{'=' * 60}")

    return {
        "processed": processed,
        "total_supplements": total_supplements,
        "elapsed_s": elapsed,
        "ok": True,
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Post-extract supplement — LLM missing-fact completion"
    )
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = supplement_pipeline(limit=args.limit, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
