"""Recovery Ladder: stdlib json.loads → json_repair for LLM output.

Single shared implementation — used by code_mod_pipeline, debate, review_worker.
"""

import json
from typing import Optional


def parse_llm_json(text: str) -> Optional[dict]:
    """Parse possibly-malformed JSON from LLM output.

    Rung 1: stdlib json.loads (95% of cases)
    Rung 2: json_repair — trailing commas, unclosed braces, single quotes, md fences, etc.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    # Strip markdown code fences (json_repair handles these too, but stdlib doesn't)
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [l for l in lines if not l.startswith("```")]
        cleaned = "\n".join(lines).strip()
    # Rung 1
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, TypeError):
        pass
    # Rung 2
    try:
        from json_repair import repair_json
        return repair_json(cleaned, return_objects=True)
    except Exception:
        return None
