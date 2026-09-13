"""Extract EDC (Extract, Describe, Classify) — prompt building and response parsing.

Re-exports from the pipeline_stages layer, keeping compatibility with
the legacy scripts/lib/extract_llm package while providing a clean
domain-focused interface.
"""
from __future__ import annotations

from typing import Any

from devforge.ports.extract import ExtractedFact, TurnData

# ── System prompts ──
SYSTEM_TEXT_EXTRACT = """You are a meticulous fact extractor.
Extract facts from the provided conversation turn.
Return JSON with a list of extracted facts."""

SYSTEM_USER_EXTRACT = """Extract facts from this user turn:
---
{user_turn}
---
{thinking}"""


def build_extract_prompt(turn: TurnData) -> list[dict[str, str]]:
    """Build the chat messages for extraction.

    Uses section-major extraction: user turn and system text are processed
    in separate sections for optimal KV cache utilization.
    """
    return [
        {"role": "system", "content": SYSTEM_TEXT_EXTRACT},
        {
            "role": "user",
            "content": SYSTEM_USER_EXTRACT.format(
                user_turn=turn.user_turn[:4000],
                thinking=turn.thinking or "",
            ),
        },
    ]


def parse_extract_response(
    content: str,
    turn_id: Any,
    extract_model: str,
) -> list[ExtractedFact]:
    """Parse LLM JSON response into ExtractedFact objects.

    Handles JSON recovery for malformed responses (uses legacy
    _clean_extraction_json logic from extract_llm package).
    """
    import json
    from uuid import UUID

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        # Use legacy JSON recovery
        try:
            from scripts.lib.extract_llm.parser import _clean_extraction_json
            cleaned = _clean_extraction_json(content)
            data = json.loads(cleaned)
        except Exception:
            data = {"facts": []}

    facts = []
    for idx, f in enumerate(data.get("facts", []) if isinstance(data, dict) else data):
        facts.append(ExtractedFact(
            turn_id=turn_id if isinstance(turn_id, UUID) else UUID(str(turn_id)),
            fact_index=idx,
            fact_type=f.get("fact_type", "text"),
            evidence=f.get("evidence", ""),
            extract_model=extract_model,
            subject=f.get("subject"),
            predicate=f.get("predicate"),
            object_=f.get("object"),
            qualifiers=f.get("qualifiers"),
        ))

    return facts
