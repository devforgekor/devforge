#!/usr/bin/env python3
# Status: production
# Path: extract.py — re-export from lib/extract_llm/
"""LLM extraction submodule — re-exports from lib/extract_llm package.

See lib/extract_llm/ for implementation in submodules:
  chunking.py — text splitting, paragraph grouping, compound expansion
  edc.py — EDC predicate/entity normalization, QC pipeline
  parser.py — JSON error recovery and parsing
  _core.py — constants, prompts, token calc, embed mgmt, _extract_edcr_freeform
"""

import os
import sys

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.extract_llm import (  # noqa: E402, F401
    _SIGTERM_RECEIVED,
    _SYSTEM_TEXT_EXTRACT_FREE,
    _SYSTEM_TEXT_EXTRACT_FREE_8B,
    _SYSTEM_USER_EXTRACT_FREE,
    _SYSTEM_USER_EXTRACT_FREE_8B,
    SYSTEM_DAY_EXTRACT,
    SYSTEM_DESCRIBE_FILE,
    SYSTEM_FALLBACK,
    _build_section_prefix,
    _calc_max_tokens,
    _calc_timeout,
    _call_with_8082_retry,
    _checkpoint_sections,
    _clean_extraction_json,
    _cleanup_all_llms,
    _extract_edcr_freeform,
    _fix_status_hallucination,
    _merge_usage,
    _parse_heading_level,
    _parse_json,
    _quality_check_facts,
    _sigterm_handler,
    _split_atomic,
)
from lib.extract_llm.chunking import (  # noqa: E402, F401
    CHUNK_OVERLAP_CHARS,
    _group_sentences,
    _emit_chunks_with_overlap,
    _merge_section_paragraphs,
)
from lib.extract_llm.edc import _normalize_freeform_pipeline  # noqa: E402, F401

_CONFLICT_RESOLVER_4B = """\
You are a Conflict Resolver. Two fact extractors each extracted a fact about the same subject.

— Fact A came from a Strict Extractor (high precision, conservative, max 4 facts).
— Fact B came from an Exploratory Extractor (high recall, includes evidence text).
— Original source text may be included as "Evidence" for one or both facts.

Decide which relationship best describes the pair:

CONSENSUS: Same claim, different wording. (e.g. "DB connection pooling missing" vs "missing DB connection pooling")
CONTRADICT: Mutually exclusive. One must be wrong. (e.g. "latency = 100ms" vs "latency = 200ms")
DIFFERENT_ASPECT: Same topic, different facet. Both can be true. (e.g. "Postgres handles DB connections" vs "Postgres uses SQLAlchemy")

RULES:
1. If Evidence confirms both facts are valid in context → DIFFERENT_ASPECT (not CONTRADICT).
2. If Evidence is absent → base judgment on the facts alone.
3. If Fact A and Fact B say the same thing with different words → CONSENSUS.
4. If one fact claims X and the other claims not-X on the same axis → CONTRADICT.
5. If they cover different attributes of the same subject → DIFFERENT_ASPECT.

English only. Return ONLY valid JSON, no extra text:

{"verdict": "CONSENSUS", "reason": "why (≤15 words)"}"""

_STRUCTURED_FIELDS = """
Structured fields — every extraction MUST have subject, predicate, object:
  subject:   The concrete entity this fact is about (file, function, config key, service, port, model).
  predicate: Free-form snake_case verb phrase capturing the relation between subject and object.
             MUST be snake_case (lowercase + underscores, 2-5 words). Describe WHAT subject DOES TO object.
             Avoid vague words like "is", "has", "does", "related_to".
  object:    The specific value, outcome, or target entity.
  qualifiers: Optional JSON for additional context (e.g., {"from": "8081"}). Omit if not needed.

Good predicate examples:
  configures_port_to    ("the server configures the port to 8082")
  replaces_with         ("the patch replaces the old implementation with the new one")
  preheats_oven_to      ("the cook preheats the oven to 180 degrees")  — any domain works
  runs_on_version       ("the service runs on version 3.2.1")
  documents_usage_in    ("the comment documents usage in README.md")

Bad predicate patterns — do NOT use:
  "is" / "has" / "does" / "was" / "are" — too vague, don't describe the specific relation
  Full sentences or phrases with spaces — use snake_case
  Multi-word descriptions that repeat subject/object (e.g. "config_set" — what config? be specific)
"""

_SYSTEM_TEXT_EXTRACT_STRICT = """\
Extract factual triples (subject, predicate, object) from the ASSISTANT RESPONSE (text).
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Prefer specific predicates. If only a generic verb like "is" or "has" fits, use it rather than skipping.
4. Self-contained: Resolve pronouns and implicit references.
5. No duplicates: Same fact extracted once only.
6. No fabrication: Only extract what is present or clearly implied in the text.
7. If uncertain about a detail, include "confidence": 0.5-0.9 in qualifiers rather than skipping.
8. Skip trivial conversation flow markers.

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<snake_case_verb>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}."""

_SYSTEM_USER_EXTRACT_STRICT = """\
Extract factual triples (subject, predicate, object) from the USER MESSAGE.
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Prefer specific predicates. If only a generic verb like "is" or "has" fits, use it rather than skipping.
4. Self-contained: Resolve pronouns ("it runs on port 8082" → "the server runs on port 8082").
5. No duplicates: Same fact extracted once only.
6. If uncertain about a detail, include "confidence": 0.5-0.9 in qualifiers rather than skipping.
7. Skip trivial conversation flow markers ("I see", "let me check", etc.).

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<snake_case_verb>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}.
Noise detection (gibberish, errors): {"skip_verdict": "skip"}."""

_SYSTEM_TEXT_EXTRACT_XPLORE = """\
You are an Exploratory Fact Extractor for a developer conversation. Extract ALL potential
factual triples (subject, predicate, object) from the ASSISTANT'S RESPONSE (text),
including strong implications.

RULES:
1. HIGH RECALL: Extract explicit facts AND strongly implied relationships.
2. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim. Split "X and Y".
3. SELF-CONTAINED: Resolve pronouns and implicit references.
4. SUBJECT-PREDICATE-OBJECT: All three required. Predicate is a snake_case verb phrase.
5. EVIDENCE: Short supporting phrase (max 12 words), grounded in source text.
6. CONFIDENCE: If ambiguous, include "confidence": 0.5-0.9 in qualifiers.
7. PREDICATE QUALITY: Use descriptive action verbs. Avoid "is" / "has" / "does" / "was".
8. DEDUP: Same fact extracted once.
9. Max 4 facts per turn. Do NOT pad.
10. Do NOT fabricate. Only extract what is present or strongly implied.
11. CRITICAL: Output JSON directly. No thinking, no reasoning, no chain-of-thought — return ONLY the JSON object.

Output STRICT JSON — same format as standard extractor:
{
  "extractions": [
    {
      "evidence": "<sentence from source>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<concrete entity>",
      "predicate": "<snake_case_verb_phrase>",
      "object": "<specific value or outcome>",
      "qualifiers": {"confidence": 0.0-1.0},
      "source_context": "<surrounding source text>"
    }
  ]
}"""

_SYSTEM_USER_EXTRACT_XPLORE = """\
You are an Exploratory Fact Extractor for a developer conversation. Extract ALL potential
factual triples (subject, predicate, object) from the USER MESSAGE, including strong implications.

RULES:
1. HIGH RECALL: Extract explicit facts AND strongly implied relationships.
2. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim. Split "X and Y".
3. SELF-CONTAINED: Resolve pronouns ("it uses port 8082" → "the LLM server uses port 8082").
4. SUBJECT-PREDICATE-OBJECT: All three required. Predicate is a snake_case verb phrase.
5. EVIDENCE: Short supporting phrase (max 12 words), grounded in source text.
6. CONFIDENCE: If ambiguous, include "confidence": 0.5-0.9 in qualifiers. Omit if certain.
7. PREDICATE QUALITY: Use descriptive action verbs. Avoid "is" / "has" / "does" / "was".
8. DEDUP: Same fact extracted once.
9. Max 4 facts per turn. Do NOT pad.
10. SIGNIFICANCE: Extract specific, non-obvious facts. Skip trivial conversation flow.
11. CRITICAL: Output JSON directly. No thinking, no reasoning, no chain-of-thought — return ONLY the JSON object.

Output STRICT JSON — same format as standard extractor:
{
  "extractions": [
    {
      "evidence": "<sentence from source>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<concrete entity>",
      "predicate": "<snake_case_verb_phrase>",
      "object": "<specific value or outcome>",
      "qualifiers": {"confidence": 0.0-1.0},
      "source_context": "<surrounding source text>"
    }
  ]
}"""

_SYSTEM_TEXT_EXTRACT_XPLORE_FREE = """\
Extract factual triples (subject, predicate, object) from the ASSISTANT RESPONSE (text).
Include explicit facts AND strongly implied relationships.

Output ONLY valid JSON. No extra text.

RULES:
1. HIGH RECALL: Extract explicit facts AND strongly implied relationships.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns and implicit references.
4. No duplicates.
5. Max 4 facts per turn. Do NOT pad.
6. Do NOT fabricate. Only extract what is present or strongly implied.
7. If ambiguous, include "confidence": 0.5-0.9 in qualifiers.
8. Predicate is descriptive verb phrase — natural language, NOT snake_case.

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}."""

_SYSTEM_USER_EXTRACT_XPLORE_FREE = """\
Extract factual triples (subject, predicate, object) from the USER MESSAGE.
Include explicit facts AND strongly implied relationships.

Output ONLY valid JSON. No extra text.

RULES:
1. HIGH RECALL: Extract explicit facts AND strongly implied relationships.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns.
4. No duplicates.
5. Max 4 facts per turn. Do NOT pad.
6. If ambiguous, include "confidence": 0.5-0.9 in qualifiers.
7. Skip trivial conversation flow markers.
8. Predicate is descriptive verb phrase — natural language, NOT snake_case.

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {},
      "source_context": "<surrounding text>"
    }
  ]
}

If nothing extractable: {"extractions": []}.
Noise detection (gibberish, errors): {"skip_verdict": "skip"}."""
