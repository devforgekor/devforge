#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.py — runs after extract.py
"""Enrich Pipeline — post-extraction metadata enrichment (tldr, intent, entities, tags).

Runs independently after extract pipeline finishes fact extraction.
Finds turns that have extraction facts but no enrichment metadata yet, then
generates enrichment fields (tldr, intent, entities, tags) for each turn.
Verify step is handled separately by day_verify.py (Phase 3 in day_cycle.py).

State-based: NOT EXISTS enrich_meta is the sole filter. Checkpoint not needed.

Usage:
  python3 scripts/pipelines/enrich.py                    # batch
  python3 scripts/pipelines/enrich.py --turn-id <uuid>   # single turn (debug)
  python3 scripts/pipelines/enrich.py --limit 20         # batch cap
  python3 scripts/pipelines/enrich.py --dry-run          # simulate, no writes
"""

import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.common import strip_think
from lib.enrich_feedback import load_enrich_feedback, format_few_shot
from lib.llm_client import call_llm
from lib.llm.json_parser import save_dlq, parse_llm_json
from lib.token_budget import TokenBudget

TIMEOUT_ENRICH = 900
MAX_TOKENS_ENRICH = 512
TEMP_ENRICH = 0.1
BATCH_LIMIT = 20
TIME_BUDGET = 3600  # default: 1 hour budget for batch slicing


def _calc_batch_limit(time_budget: int) -> int:
    """Estimate how many turns fit within time_budget for sequential enrich.

    Per-turn estimate: prompt decode + LLM gen + reranker overhead.
    """
    from lib.db import psql_json

    sql = (
        "SELECT LENGTH(COALESCE(t.user_turn,'')) "
        "  + LENGTH(COALESCE(t.text,'')) AS total_chars "
        "FROM turns t "
        "WHERE EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id "
        "  AND rf.fact_type IN ('user','thinking','text')"
        ")"
        "AND NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf2 "
        "  WHERE rf2.turn_id = t.id AND rf2.fact_type = 'enrich_meta'"
        ")"
        "ORDER BY t.created_at ASC"
    )
    rows = psql_json(sql)
    if not rows:
        return 0

    total_est = 0.0
    count = 0
    for row in rows:
        chars = row.get("total_chars", 0) or 0
        est = min(60 + int(chars * 0.3) + 120, TIMEOUT_ENRICH)
        if total_est + est > time_budget:
            break
        total_est += est
        count += 1

    return max(count, 1)

SYSTEM_DAY_ENRICH = """\
You are a conversation analyst preparing structured metadata for an MCP
(Model Context Protocol) system. Given the original conversation turn and
the extracted facts, produce structured MCP fields.

CRITICAL: Only include entities that are EXPLICITLY named in the conversation.
Do NOT infer or assume technologies, files, or functions that the user did
not mention. When unsure, use an empty array [].

Output STRICT JSON:
{
  "tldr": "One-line summary (max 15 words) — what this turn is about",
  "intent": "question|request|report|clarification|code_change|debug|design|other",
  "category": "requirement|decision|explanation|code|reasoning|other",
  "entities": {
    "files": [],
    "technologies": [],
    "functions": [],
    "mentioned_users": []
  },
  "tags": []
}

Rules:
- tldr must be factual and directly derivable from the turn content
- intent must be one of the enumerated values
- category: classify the turn's primary nature — requirement (new ask), decision (choice made), explanation (how/why), code (implementation), reasoning (analysis), other
- entities.files: only include file paths EXPLICITLY named in the turn
- entities.technologies: only technologies EXPLICITLY named; do NOT assume common tools like Python, FastAPI unless the user said them
- entities.functions: function/class/method names EXPLICITLY mentioned
- tags: 2-5 keywords for discovery and routing
- Use the extracted facts section to inform category and entity accuracy
- If a field has no relevant data, use an empty array []"""

SYSTEM_ENRICH_VERIFY = """\
You are an MCP metadata verifier. Your job is to check the generated MCP fields
against the original conversation turn and fix errors.

Given:
  === TURN ===
  user_turn / thinking / text

  === GENERATED MCP ===
  tldr / intent / entities / tags

Check each field:
  1. tldr: Accurate? Max 15 words? No markdown? If wrong, fix.
  2. intent: Matches the turn? Must be one of:
     question|request|report|clarification|code_change|debug|design|other
  3. category: Matches the turn's primary nature? Must be one of:
     requirement|decision|explanation|code|reasoning|other
  4. entities.files: Only include files EXPLICITLY mentioned in the turn.
  5. entities.technologies: Technologies actually discussed.
  6. entities.functions: Function names actually mentioned.
  7. tags: Relevant to the turn? Max 5 tags.

Output corrected MCP JSON — same schema, only fix what's wrong.
Include a verdict block showing what changed.

Output:
{
  "tldr": "corrected summary",
  "intent": "corrected intent",
  "category": "corrected category",
  "entities": {"files":[], "technologies":[], "functions":[], "mentioned_users":[]},
  "tags": ["tag1", "tag2"],
  "verdict": {"changes_made": false, "tldr_changed": false,
              "intent_changed": false, "category_changed": false,
              "entities_changed": false, "tags_changed": false}
}"""

_VALID_INTENTS = {"question", "request", "report", "clarification",
                  "code_change", "debug", "design", "other"}
_VALID_CATEGORIES = {"requirement", "decision", "explanation",
                     "code", "reasoning", "other"}

def _parse_json(raw: str, label: str = "enrich", attempt: int = 1) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM output using shared parse_llm_json + DLQ."""
    cleaned = strip_think(raw)
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(raw, stage=f"enrich_{label}", error="parse_llm_json returned None",
                 attempt=attempt)
    return result


def _clean_markdown(text: str) -> str:
    """Strip all markdown formatting from text."""
    if not text:
        return text
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'``.*?``', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*{2,}([^*]+)\*{2,}', r'\1', text)
    text = re.sub(r'_{2,}([^_]+)_{2,}', r'\1', text)
    text = re.sub(r'~{2,}([^~]+)~{2,}', r'\1', text)
    text = text.replace('`', '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _clean_markdown(text: str) -> str:
    """Remove markdown formatting from text."""
    if not text:
        return ""
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*{2,}([^*]+)\*{2,}', r'\1', text)
    text = re.sub(r'_{2,}([^_]+)_{2,}', r'\1', text)
    text = re.sub(r'~{2,}([^~]+)~{2,}', r'\1', text)
    text = text.replace('`', '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _post_process_enrich(enrich_data: Optional[Dict[str, Any]],
                      user_turn: str = "", text: str = ""
                      ) -> Optional[Dict[str, Any]]:
    """Python post-processing for enrichment fields: validate, clean, trim, structural filter."""
    if not enrich_data:
        return enrich_data

    # tldr
    tldr = enrich_data.get("tldr", "")
    if tldr:
        tldr = _clean_markdown(tldr)
        words = tldr.split()
        if len(words) > 20:
            tldr = " ".join(words[:20]) + "..."
    enrich_data["tldr"] = tldr[:200] if tldr else ""

    # intent
    intent = enrich_data.get("intent", "").lower()
    if intent not in _VALID_INTENTS:
        enrich_data["intent"] = "other"

    # category (new field)
    category = enrich_data.get("category", "").lower()
    if category not in _VALID_CATEGORIES:
        enrich_data["category"] = "other"

    # entities — with structural pre-filter
    entities = enrich_data.get("entities", {})
    if not isinstance(entities, dict):
        entities = {}
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = entities.get(key, [])
        if not isinstance(items, list):
            items = []
        seen: set = set()
        clean_items = []
        for item in items:
            s = str(item).strip()
            if not s or s in seen:
                continue
            # Filter entities that cannot represent valid code symbols
            if len(s) < _MIN_ENTITY_LEN:
                continue
            if key != "files" and _ENTITY_SPECIAL_CHARS.search(s):
                continue
            if any(p.search(s) for p in _ENTITY_REJECT_PATTERNS):
                continue
            if len(s.split()) > _MAX_ENTITY_WORDS:
                continue
            seen.add(s)
            if key == "files":
                s = s.lstrip("./")
            clean_items.append(s)
        entities[key] = clean_items[:10]
    enrich_data["entities"] = entities

    # tags
    tags = enrich_data.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    seen_tags: set = set()
    clean_tags = []
    for tag in tags:
        t = str(tag).strip().lower()
        if t and t not in seen_tags:
            seen_tags.add(t)
            clean_tags.append(t)
    enrich_data["tags"] = clean_tags[:5]

    # Tag-intent consistency
    intent = enrich_data.get("intent", "other")
    blocked = _INTENT_TAG_BLOCKED.get(intent, set())
    if blocked:
        enrich_data["tags"] = [t for t in enrich_data.get("tags", []) if t not in blocked]

    return enrich_data


_ENTITY_REJECT_PATTERNS = [
    re.compile(r'https?://\S+'),
    re.compile(r'ftp://\S+'),
    re.compile(r'ftp\b'),
    re.compile(r'[\[\](){}]'),
    re.compile(r'^[\d\s]+$'),
]
_MAX_ENTITY_WORDS = 8
_MIN_ENTITY_LEN = 2
_ENTITY_SPECIAL_CHARS = re.compile(r'[@#$%^&*+=<>|\\~`;]')

# Tag-intent consistency
_INTENT_TAG_BLOCKED = {
    "question": {"code_change", "implementation", "refactor", "deploy"},
    "request": {"debug", "bug"},
    "clarification": {"implementation", "code_change", "deploy", "bug"},
    "code_change": {"question", "help", "howto", "debug"},
    "debug": {"feature", "design", "proposal"},
    "design": {"bug", "debug", "hotfix"},
    "report": {"question", "howto"},
    "other": set(),
}


def _generate_enrich_fields(user_turn: str, thinking: str, text: str,
                         model: str = "day_enrich",
                         extractions: Optional[List[Dict]] = None
                         ) -> Optional[Dict[str, Any]]:
    """Generate enrichment metadata fields (tldr, intent, entities, tags) via *model*.

    Uses TokenBudget priority allocation — sections dropped entirely if
    budget exceeded (no partial truncation).

    Returns dict with enrich fields + usage/timings metadata.
    """
    budget = TokenBudget("enrich")
    parts = ["=== user_turn ==="]
    if budget.add_section("user_turn", user_turn or "(empty)", priority=10):
        parts.append(user_turn or "(empty)")

    parts.append("")
    parts.append("=== thinking ===")
    if budget.add_section("thinking", thinking or "(empty)", priority=4):
        parts.append(thinking or "(empty)")

    parts.append("")
    parts.append("=== text ===")
    if budget.add_section("text", text or "(empty)", priority=7):
        parts.append(text or "(empty)")

    if extractions:
        parts.append("")
        parts.append("=== extracted facts ===")
        for idx, ex in enumerate(extractions):
            confidence = ex.get('fact_confidence', 100)
            line = f"  [{ex.get('fact_type','?')}] (conf={confidence}) {ex.get('evidence','')[:300]}"
            if budget.add_section(f"extract_{idx}", line, priority=6):
                parts.append(line)
    parts.append("")
    if budget.used > 0:
        parts.append(f"[context budget: {budget.used}/{budget.limit} tok]")

    # Inject few-shot feedback from verify_result collection
    system_content = SYSTEM_DAY_ENRICH
    feedback_examples = load_enrich_feedback()
    if feedback_examples:
        feedback_text = format_few_shot(feedback_examples)
        if feedback_text:
            system_content = SYSTEM_DAY_ENRICH + "\n\n" + feedback_text

    meta = call_llm(
        [{"role": "system", "content": system_content},
         {"role": "user", "content": "\n".join(parts)}],
        model=model,
        max_tokens=MAX_TOKENS_ENRICH, temperature=TEMP_ENRICH, timeout=TIMEOUT_ENRICH,
        json_mode=True, return_meta=True,
    )
    result = _parse_json(meta["content"], "enrich fields")
    if result:
        result["_meta"] = {"usage": meta["usage"], "timings": meta["timings"],
                           "elapsed_ms": meta["elapsed_ms"], "model": model}
    return result




def _get_turns_without_enrich(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Return turns with extraction facts but no enrich_meta.
    State-based: NOT EXISTS is the sole filter.
    """
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, t.created_at "
        "FROM turns t "
        # Has extraction facts (not enrich_meta itself)
        "WHERE EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id "
        "  AND rf.fact_type IN ('user','thinking','text')"
        ")"
        # Doesn't already have enrich_meta
        "AND NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf2 "
        "  WHERE rf2.turn_id = t.id AND rf2.fact_type = 'enrich_meta'"
        ")"
        "ORDER BY t.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    return [{
        "id": r.get("id", ""),
        "user_turn": r.get("user_turn", ""),
        "thinking": r.get("thinking") or None,
        "text": r.get("text", ""),
        "created_at": r.get("created_at", ""),
    } for r in rows]


def _get_turn_extractions(turn_id: str) -> List[Dict[str, Any]]:
    """Load extracted facts (user/thinking/text) for a turn from review_facts.

    Returns list of dicts with fact_type and evidence keys,
    ordered by fact_index. Used as input context for MCP generation.
    """
    sql = (
        f"SELECT fact_type, evidence, fact_confidence "
        f"FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND fact_type IN ('user','thinking','text') "
        f"ORDER BY fact_index ASC"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    return [{
        "fact_type": r.get("fact_type", "text"),
        "evidence": r.get("evidence", ""),
        "fact_confidence": r.get("fact_confidence", 100),
    } for r in rows]


def _insert_enrich_fact(turn_id: str, fact_index: int,
                     enrich_json_str: str, model: str,
                     prompt_tokens: Optional[int] = None,
                     gen_tokens: Optional[int] = None,
                     elapsed_ms: Optional[float] = None,
                     source_file: Optional[str] = None) -> bool:
    """Insert an enrich_meta fact row into review_facts."""
    cols = ["turn_id", "fact_index", "fact_type", "evidence", "extract_model",
            "verdict", "source", "fact_action", "fact_confidence"]
    vals = [
        f"'{esc_sql(turn_id)}'::uuid",
        str(fact_index),
        "'enrich_meta'",
        f"'{esc_sql(enrich_json_str[:5000])}'",
        f"'{esc_sql(model)}'",
        "'pending'",
        "'enrich'",
        "'enrich_meta'",
        "100",
    ]
    set_clauses = []

    if prompt_tokens is not None:
        cols.append("prompt_tokens")
        vals.append(str(prompt_tokens))
        set_clauses.append(f"prompt_tokens = {prompt_tokens}")
    if gen_tokens is not None:
        cols.append("gen_tokens")
        vals.append(str(gen_tokens))
        set_clauses.append(f"gen_tokens = {gen_tokens}")
    if elapsed_ms is not None:
        cols.append("elapsed_ms")
        vals.append(f"{elapsed_ms:.1f}")
        set_clauses.append(f"elapsed_ms = {elapsed_ms:.1f}")
    if source_file:
        cols.append("source_file")
        vals.append(f"'{esc_sql(source_file)}'")
        set_clauses.append(f"source_file = '{esc_sql(source_file)}'")

    sql = (
        f"INSERT INTO review_facts ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)}) "
        f"ON CONFLICT (turn_id, fact_index, extract_model) "
        f"DO UPDATE SET evidence = EXCLUDED.evidence"
        + (f", {', '.join(set_clauses)}" if set_clauses else "")
    )
    return psql_ok(sql)


def enrich_pipeline(turn_id: Optional[str] = None,
                        limit: int = BATCH_LIMIT,
                        dry_run: bool = False,
                        model: str = "day_enrich",
                        time_budget: Optional[int] = None) -> Dict[str, Any]:
    """Generate enrichment metadata for turns with extraction facts."""
    t_start = time.monotonic()
    print(f"\n{'=' * 60}")
    print(f"Enrich Pipeline — {model} enrich fields for extracted turns")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    if time_budget:
        print(f"  Time budget: {time_budget}s")
    print(f"{'=' * 60}")

    # Select turns
    if turn_id:
        sql = (
            "SELECT t.id, t.user_turn, t.thinking, t.text, t.created_at "
            f"FROM turns t WHERE t.id = '{esc_sql(turn_id)}'::uuid"
        )
        rows = psql_json(sql)
        if not rows:
            print(f"[enrich] Turn not found: {turn_id}")
            return {"processed": 0, "failed": 1, "ok": False}
        r = rows[0]
        turns = [{
            "id": r["id"], "user_turn": r.get("user_turn", ""),
            "thinking": r.get("thinking") or None,
            "text": r.get("text", ""),
            "created_at": r.get("created_at", ""),
        }]
    else:
        if time_budget:
            limit = _calc_batch_limit(time_budget)
            print(f"  Budget-limited batch: {limit} turns")
        turns = _get_turns_without_enrich(limit)

    if not turns:
        print("[enrich] No turns without enrichment found")
        return {"processed": 0, "failed": 0, "ok": True}

    print(f"[enrich] Processing {len(turns)} turn(s)", flush=True)

    processed = 0
    failed = 0

    for ti, turn in enumerate(turns, 1):
        turn_id = turn["id"]
        user_turn = turn.get("user_turn", "") or ""
        thinking = turn.get("thinking", "") or ""
        text = turn.get("text", "") or ""

        print(f"  [{ti}/{len(turns)}] {turn_id[:8]}", flush=True)

        try:
            # Phase 0: Load extraction facts for enrichment context
            extractions = _get_turn_extractions(turn_id)
            if extractions:
                print(f"    extractions: {len(extractions)} facts loaded", flush=True)
            else:
                print(f"    extractions: none found", flush=True)

            # Phase 1: Generate enrichment fields with extraction context
            print(f"    Enrich generation ({model})...", flush=True)
            enrich_result = _generate_enrich_fields(
                user_turn, thinking, text, model=model,
                extractions=extractions,
            )
            if not enrich_result:
                print(f"    Enrich generation returned None — skipping", flush=True)
                failed += 1
                continue

            # Phase 2: Python post-processing
            enrich_result = _post_process_enrich(enrich_result, user_turn, text)

            # Phase 3: Log enrichment fields
            enrich_tldr = enrich_result.get("tldr", "") or ""
            enrich_intent = enrich_result.get("intent", "other") or "other"
            enrich_category = enrich_result.get("category", "other") or "other"
            enrich_entities = enrich_result.get("entities", {}) or {}
            enrich_tags = enrich_result.get("tags", []) or []
            if enrich_tldr:
                print(f"    tldr: {enrich_tldr}", flush=True)
            if enrich_intent:
                print(f"    intent: {enrich_intent}", flush=True)
            if enrich_category:
                print(f"    category: {enrich_category}", flush=True)
            if enrich_entities:
                print(f"    entities: files={len(enrich_entities.get('files',[]))}, "
                      f"funcs={len(enrich_entities.get('functions',[]))}", flush=True)
            if enrich_tags:
                print(f"    tags: {enrich_tags}", flush=True)

            # Phase 4: Store
            if dry_run:
                print(f"    [DRY] Would store enrichment fields for {turn_id[:8]}", flush=True)
                processed += 1
                continue

            # Find the next fact_index for this turn
            fi_sql = (
                f"SELECT COALESCE(MAX(fact_index), -1) + 1 "
                f"FROM review_facts WHERE turn_id = '{esc_sql(turn_id)}'::uuid"
            )
            fi_str = psql(fi_sql)
            fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0

            # Strip _meta from stored result
            enrich_meta = enrich_result.get("_meta", {})
            enrich_prompt_tokens = enrich_meta.get("usage", {}).get("prompt_tokens") if enrich_meta else None
            enrich_gen_tokens = enrich_meta.get("usage", {}).get("completion_tokens") if enrich_meta else None
            enrich_elapsed_ms = enrich_meta.get("elapsed_ms") if enrich_meta else None

            enrich_for_storage = {k: v for k, v in enrich_result.items() if k != "_meta"}

            _insert_enrich_fact(turn_id, fi, json.dumps(enrich_for_storage, ensure_ascii=False),
                             model,
                             prompt_tokens=enrich_prompt_tokens,
                             gen_tokens=enrich_gen_tokens,
                             elapsed_ms=enrich_elapsed_ms)
            print(f"    Stored enrich_meta (fact_index={fi})", flush=True)
            processed += 1

        except Exception as e:
            print(f"    ERROR: {type(e).__name__}: {e}", flush=True)
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}", flush=True)
    print(f"Done: {processed} enriched, {failed} failed ({elapsed}s)", flush=True)
    if dry_run:
        print("  [DRY RUN] No data was written", flush=True)
    print(f"{'=' * 60}", flush=True)

    return {"processed": processed, "failed": failed,
            "elapsed_s": elapsed, "ok": failed == 0}


def main() -> None:
    from lib.infra.preflight import preflight_checks
    preflight_checks("enrich.py")
    import argparse
    parser = argparse.ArgumentParser(
        description="Enrich Pipeline — generate enrichment fields for extracted turns")
    parser.add_argument("--turn-id", help="Process a specific turn UUID")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--time-budget", "-t", type=int, default=None,
                        help="Max estimated processing seconds for batch (default: no limit)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--model", default="day_enrich",
                        help="Model for enrichment fields generation (default: day_enrich)")
    args = parser.parse_args()

    result = enrich_pipeline(
        turn_id=args.turn_id,
        limit=args.limit,
        dry_run=args.dry_run,
        model=args.model,
        time_budget=args.time_budget,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
