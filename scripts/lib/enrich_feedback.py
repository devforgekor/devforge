#!/usr/bin/env python3
# Status: production
# Path: imported by — watchdog.py (periodic update), enrich.py (read-only)
"""Enrich Feedback — collect day_verify.py verify_result for few-shot injection.

Architecture::

    watchdog (day mode, 10min throttle)
      → collect_verify_feedback()
        → SELECT review_facts WHERE fact_type='verify_result'
        → extract UNGROUNDED entities + GROUNDED examples
        → write /opt/ai_data/enrich_fewshot.json

    enrich.py (_generate_enrich_fields)
      → load_enrich_feedback()
        → read /opt/ai_data/enrich_fewshot.json
        → inject as few-shot into SYSTEM_DAY_ENRICH

The feedback file is a simple JSON with examples that the LLM can learn from.
No LLM calls involved — pure Python + DB query.
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

from lib.db import psql_json

FEEDBACK_FILE = "/opt/ai_data/enrich_fewshot.json"
THROTTLE_SEC = 600       # 10 min between file updates
MAX_EXAMPLES_BAD = 4     # max UNGROUNDED examples per entity type
MAX_EXAMPLES_GOOD = 2    # max GROUNDED examples per entity type
LOOKBACK_HOURS = 72      # query last 72h for verify results

# Score thresholds (actual scores are 0.0-1.0 from LLM verify)
GOOD_SCORE_MIN = 0.8     # min score to be a "good" (grounded) example
BAD_SCORE_MAX = 0.4      # max score to be a "bad" (ungrounded) example

# Grounding values that count as failure
UNGROUNDED_VALUES = {"UNGROUNDED", "AMBIGUOUS"}


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{ts}] [enrich_feedback] {msg}", flush=True)


# ── DB query ────────────────────────────────────────────────────────

def _fetch_verify_results() -> List[Dict[str, Any]]:
    """Fetch recent verify_result rows from review_facts.

    Returns list with turn source text included for context.
    """
    sql = (
        "SELECT rf.evidence::text AS evidence_str, "
        "  t.user_turn, t.text, "
        "  rf.created_at::text "
        "FROM review_facts rf "
        "JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.fact_type = 'verify_result' "
        f"  AND rf.created_at > now() - interval '{LOOKBACK_HOURS} hours' "
        "ORDER BY rf.created_at DESC "
        "LIMIT 100"
    )
    rows = psql_json(sql)
    return [
        {
            "evidence_str": r.get("evidence_str", "{}"),
            "user_turn": r.get("user_turn", ""),
            "text": r.get("text", ""),
            "created_at": r.get("created_at", ""),
        }
        for r in (rows or [])
    ]


def _extract_examples(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
    """Extract bad (UNGROUNDED) and good (GROUNDED) examples from verify results.

    Returns {"bad": [...], "good": [...]}.
    """
    bad: List[Dict] = []
    good: List[Dict] = []

    for row in rows:
        try:
            verify = json.loads(row.get("evidence_str", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue

        faithfulness = verify.get("faithfulness", {})
        if not isinstance(faithfulness, dict):
            continue

        # Per-entity-type iteration
        entity_types = ["files", "technologies", "functions", "mentioned_users"]
        for etype in entity_types:
            items = faithfulness.get(etype, [])
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                entity = str(item.get("entity", "")).strip()
                score = item.get("score")
                if not entity or score is None:
                    continue
                grounding = str(item.get("grounding", ""))
                method = str(item.get("method", ""))

                if grounding in UNGROUNDED_VALUES and score < BAD_SCORE_MAX:
                    bad.append({
                        "type": "bad",
                        "entity_type": etype,
                        "entity": entity[:80],
                        "score": score,
                        "grounding": grounding,
                        "method": method,
                        "lesson": (
                            f"Entity '{entity}' (type: {etype}) was not grounded "
                            f"in source text (score {score}, {grounding}). "
                            f"Only include entities explicitly mentioned in the conversation."
                        ),
                    })
                elif grounding == "GROUNDED" and score >= GOOD_SCORE_MIN:
                    good.append({
                        "type": "good",
                        "entity_type": etype,
                        "entity": entity[:80],
                        "score": score,
                        "grounding": grounding,
                        "method": method,
                        "lesson": (
                            f"Entity '{entity}' (type: {etype}) correctly identified "
                            f"(score {score}, {grounding}). "
                            f"This entity is directly mentioned in the conversation."
                        ),
                    })

        # tldr faithfulness — per-turn, not per-entity
        tldr = faithfulness.get("tldr", {})
        if isinstance(tldr, dict):
            tscore = tldr.get("score")
            tgrounding = str(tldr.get("grounding", ""))
            if tscore is not None:
                if tgrounding in UNGROUNDED_VALUES and tscore < BAD_SCORE_MAX:
                    bad.append({
                        "type": "bad",
                        "entity_type": "tldr",
                        "entity": str(tldr.get("text", ""))[:80],
                        "score": tscore,
                        "grounding": tgrounding,
                        "method": str(tldr.get("method", "")),
                        "lesson": (
                            f"TLDR summary not faithful to source text "
                            f"(score {tscore}, {tgrounding}). "
                            f"Keep TLDR factually grounded in the actual conversation."
                        ),
                    })
                elif tgrounding == "GROUNDED" and tscore >= GOOD_SCORE_MIN:
                    good.append({
                        "type": "good",
                        "entity_type": "tldr",
                        "entity": str(tldr.get("text", ""))[:80],
                        "score": tscore,
                        "grounding": tgrounding,
                        "method": str(tldr.get("method", "")),
                        "lesson": (
                            f"TLDR summary faithful to source text "
                            f"(score {tscore}, {tgrounding}). Good."
                        ),
                    })

    return {"bad": bad[:MAX_EXAMPLES_BAD], "good": good[:MAX_EXAMPLES_GOOD]}


# ── File read/write ─────────────────────────────────────────────────

def load_enrich_feedback() -> Optional[Dict[str, List[Dict]]]:
    """Read the feedback file. Returns None if missing or broken."""
    try:
        with open(FEEDBACK_FILE) as f:
            data = json.load(f)
        examples = data.get("examples", {})
        if not examples.get("bad") and not examples.get("good"):
            return None
        return examples
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _is_throttled() -> bool:
    """Check if file was updated within THROTTLE_SEC seconds."""
    try:
        age = time.time() - os.path.getmtime(FEEDBACK_FILE)
        return age < THROTTLE_SEC
    except OSError:
        return False


def collect_verify_feedback() -> Dict[str, List[Dict]]:
    """Main entry point: query DB and extract examples.

    Called periodically by watchdog. Updates the feedback file on disk.
    No-op if throttled (last write < 10 min ago).

    Returns extracted examples dict for callers that want immediate access.
    """
    if _is_throttled():
        return load_enrich_feedback() or {"bad": [], "good": []}

    rows = _fetch_verify_results()
    if not rows:
        log("No verify results found in last %dh" % LOOKBACK_HOURS)
        return {"bad": [], "good": []}

    examples = _extract_examples(rows)

    # Dedup by (entity_type, entity) keeping first occurrence
    seen: set = set()
    for key in ("bad", "good"):
        deduped = []
        for ex in examples.get(key, []):
            dedup_key = (ex["entity_type"], ex["entity"])
            if dedup_key not in seen:
                seen.add(dedup_key)
                deduped.append(ex)
        examples[key] = deduped

    # Write to file
    try:
        os.makedirs(os.path.dirname(FEEDBACK_FILE), exist_ok=True)
        with open(FEEDBACK_FILE, "w") as f:
            json.dump({
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "examples": examples,
            }, f, indent=2, ensure_ascii=False)
        log("Updated feedback file: %d bad, %d good examples" %
            (len(examples.get("bad", [])), len(examples.get("good", []))))
    except OSError as e:
        log(f"Failed to write feedback file: {e}")

    return examples


# ── Format for injection into system prompt ─────────────────────────

def format_few_shot(examples: Dict[str, List[Dict]]) -> str:
    """Format examples as a text block for injection into SYSTEM_DAY_ENRICH.

    Returns empty string if no examples available.
    """
    bad = examples.get("bad", [])
    good = examples.get("good", [])

    if not bad and not good:
        return ""

    parts: List[str] = []

    if bad:
        parts.append("### [FEEDBACK — Previous enrichment errors to avoid]")
        parts.append("")
        parts.append(
            "The following entities were previously marked as UNGROUNDED "
            "by our verification system. Do NOT generate these in your output:"
        )
        parts.append("")
        for ex in bad:
            parts.append(
                f"- Entity \"{ex['entity']}\" (type: {ex['entity_type']}) "
                f"— score {ex['score']}, {ex['grounding']}"
            )
        parts.append("")

    if good:
        parts.append("### [FEEDBACK — Previous enrichment successes to follow]")
        parts.append("")
        parts.append(
            "The following entities were correctly identified as GROUNDED. "
            "Follow these patterns:"
        )
        parts.append("")
        for ex in good:
            parts.append(
                f"- Entity \"{ex['entity']}\" (type: {ex['entity_type']}) "
                f"— score {ex['score']}, {ex['grounding']}"
            )
        parts.append("")

    return "\n".join(parts)
