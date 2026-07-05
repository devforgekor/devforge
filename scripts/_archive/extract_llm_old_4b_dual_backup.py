#!/usr/bin/env python3
# Status: production
# Path: extract.py — submodule for LLM extraction
"""LLM extraction submodule for the Extract Pipeline.

Contains all LLM interaction code: system prompts, 8082 recovery,
_extract_section, _extract_single, _extract_for_turn,
_extract_edcr_freeform (section-major KV cache batch, EDC+R pipeline),
JSON parsing, entity context loading, low-value filter.
"""

import json
import math
import os
import re
import sys
import threading
import time
from typing import Any, Dict, Generator, List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import context_limit, strip_think
from lib.db import esc_sql, psql_json
from lib.llm.json_parser import parse_llm_json, save_dlq
from lib.llm_client import call_llm
from lib.model_registry import MODEL_METADATA
from lib.pod_manager import ensure_model as _ensure_model_pod
from lib.watchdog.messenger import heartbeat

_8082_RECOVERY_LOCK = threading.Lock()

# ── 8082 Auto-Recovery ──────────────────────────────────────────

_CONNECTION_ERROR_SUBSTRINGS = (
    "Remote end closed",
    "Connection reset",
    "Connection refused",
    "Broken pipe",
    "RemoteDisconnected",
)


def _is_8082_connection_error(e: Exception) -> bool:
    err = str(e)
    if "8082" not in err and "extractor" not in err:
        return False
    return any(s in err for s in _CONNECTION_ERROR_SUBSTRINGS)


def _recover_8082() -> None:
    if not _8082_RECOVERY_LOCK.acquire(blocking=False):
        print("  [recovery] Another recovery in progress, waiting...", flush=True)
        _8082_RECOVERY_LOCK.acquire(blocking=True)
        print("  [recovery] Recovery finished by other thread", flush=True)
        _8082_RECOVERY_LOCK.release()
        return
    try:
        print("  [recovery] Reloading 8082...", flush=True)
        _ensure_model_pod("day-extractor", skip_if_healthy=False)
        print("  [recovery] 8082 ready", flush=True)
    except Exception as recover_err:
        print(f"  [recovery] 8082 reload failed: {recover_err}", flush=True)
    finally:
        _8082_RECOVERY_LOCK.release()


def _call_with_8082_retry(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if _is_8082_connection_error(e):
            print(f"  [recovery] 8082 error: {type(e).__name__}", flush=True)
            _recover_8082()
            return fn(*args, **kwargs)
        raise


def _call_with_8083_retry(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        err = str(e)
        if "8083" in err or "extractor-b" in err:
            print(f"  [recovery] 8083 error: {type(e).__name__}", flush=True)
        raise


# ── Constants ────────────────────────────────────────────────────

TIMEOUT_EXTRACT = 900
MAX_TOKENS_BASE = 768  # 30B MoE verbose JSON needs headroom
TOKENS_PER_300CH = 50  # 300ch당 약 1 fact 추가, ceil 적용
TEMP_EXTRACT = 0.0
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.2
TIMEOUT_PER_TOK = 1.2  # ~0.83 tok/s decode (20% safety margin)
GEN_TIME_BUF = 90  # spike/GC/swap buffer
CAP = 1800  # hard cap (절대 초과 금지)
MIN_USEFUL_TOKENS = 256  # 이 미만이면 명시적 거절
MAX_CHARS_SOLO = 5000
MAX_INPUT_CHARS_ADVERTISED = 6500  # API 에러 메시지용 광고 한계 (실제 6,714 여유)

_SIGTERM_RECEIVED = threading.Event()


def _sigterm_handler(signum, frame):
    try:
        pid = os.getpid()
        chain = []
        for _ in range(5):
            try:
                with open(f"/proc/{pid}/status") as f:
                    for line in f:
                        if line.startswith("Name:"):
                            chain.append(line.split(":", 1)[1].strip())
                        elif line.startswith("PPid:"):
                            pid = int(line.split(":", 1)[1].strip())
                            break
            except (OSError, ValueError):
                break
        print(f"\n  [SIGTERM] from parent chain: {' > '.join(chain)}", flush=True)
    except Exception:
        print("\n  [SIGTERM] (source chain unavailable)", flush=True)
    _SIGTERM_RECEIVED.set()


# ── Structured predicate fields ─────────────────────────────────

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

# ── Section-specific System prompts ─────────────────────────────

_SYSTEM_USER_EXTRACT = """\
You are a fact extractor for a developer conversation. Extract factual triples
(subject, predicate, object) that are EXPLICITLY stated in the USER MESSAGE.

Do NOT infer, summarize, or add information not present in the source.

RULES:
1. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim.
   "X and Y" → split into two entries with separate evidence.
   BAD: "the API returns 200 and the rate limit is 100"
   GOOD: "the API returns status code 200" (one entry)
   GOOD: "the rate limit is set to 100 per minute" (separate entry)

2. SELF-CONTAINED: Resolve pronouns ("it", "this", "that") and implicit references.
   "it uses port 8082" → "the LLM server uses port 8082"

3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a
   snake_case verb phrase describing the relation (see STRUCTURED FIELDS above).

4. SIGNIFICANCE: Extract only specific, non-obvious, informative facts.
   Do NOT extract trivial statements about conversation flow.

5. FAITHFULNESS: Directly traceable to source text. NO inference or hallucination.

6. CONCISE: Keep evidence under 12 words. Short, direct sentences only.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "<exact quote or sentence from source>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<concrete entity>",
      "predicate": "<snake_case_verb_phrase>",
      "object": "<specific value or outcome>",
      "qualifiers": {},
      "source_context": "<source text surrounding the evidence>"
    }
  ]
}

- Extract at least 1 fact if there is meaningful content.
- If nothing extractable, return {"extractions": []}.
- NOISE DETECTION: keyboard smash, gibberish, API error messages,
  meaningless text → return {"skip_verdict": "skip"}."""

_SYSTEM_THINKING_EXTRACT = """\
You are a fact extractor for a developer conversation. Extract factual triples
(subject, predicate, object) that are EXPLICITLY stated in the ASSISTANT'S
INTERNAL REASONING (thinking).

Do NOT infer, summarize, or add information not present in the source.

RULES:
1. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim.
   "X and Y" → split into two entries with separate evidence.

2. SELF-CONTAINED: Resolve pronouns ("it", "this", "that") and implicit references.
   "add an index" → "the user requested adding a database index"

3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a
   snake_case verb phrase describing the relation (see STRUCTURED FIELDS above).

4. SIGNIFICANCE: Extract only specific, non-obvious, informative facts.
   Do NOT extract trivial statements about conversation flow.

5. FAITHFULNESS: Directly traceable to source text. NO inference or hallucination.

6. CONCISE: Keep evidence under 12 words. Short, direct sentences only.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "<exact quote or sentence from source>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<concrete entity>",
      "predicate": "<snake_case_verb_phrase>",
      "object": "<specific value or outcome>",
      "qualifiers": {},
      "source_context": "<source text surrounding the evidence>"
    }
  ]
}

- Extract at least 1 fact if there is meaningful content.
- If thinking is empty or contains only formatting, return {"extractions": []}.
- NOISE DETECTION: keyboard smash, gibberish, API error messages,
  meaningless text → return {"skip_verdict": "skip"}."""

_SYSTEM_TEXT_EXTRACT = """\
You are a fact extractor for a developer conversation. Extract factual triples
(subject, predicate, object) that are EXPLICITLY present in the ASSISTANT'S
RESPONSE (text).

Do NOT infer, summarize, or add information not present in the source.

RULES:
1. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim.
   "X and Y" → split into two entries with separate evidence.

2. SELF-CONTAINED: Resolve pronouns ("it", "this", "that") and implicit references.
   "port 8082" → "the inference extractor runs on port 8082"

3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a
   snake_case verb phrase describing the relation (see STRUCTURED FIELDS above).

4. SIGNIFICANCE: Extract only specific, non-obvious, informative facts.
   Do NOT extract trivial statements about conversation flow.

5. FAITHFULNESS: Directly traceable to source text. NO inference or hallucination.

6. CONCISE: Keep evidence under 12 words. Short, direct sentences only.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "<exact quote or sentence from source>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<concrete entity>",
      "predicate": "<snake_case_verb_phrase>",
      "object": "<specific value or outcome>",
      "qualifiers": {},
      "source_context": "<source text surrounding the evidence>"
    }
  ]
}

- Extract at least 1 fact if there is meaningful content.
- If nothing extractable, return {"extractions": []}.
- NOISE DETECTION: keyboard smash, gibberish, API error messages,
  meaningless text → return {"skip_verdict": "skip"}."""

# ── A-Strict (4B Precision) Prompts ────────────────────────────────

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

# ── B-Xplore (Exploratory) Prompts ────────────────────────────────

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

# ── TAXONOMY-TRAP-FREE PROMPTS (no snake_case constraints) ─────────

_SYSTEM_USER_EXTRACT_FREE = """\
Extract factual triples (subject, predicate, object) from the USER MESSAGE.
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns ("it runs on port 8082" -> "the server runs on port 8082").
4. No duplicates: Same fact extracted once only.
5. If uncertain, include "confidence": 0.5-0.9 in qualifiers rather than skipping.
6. Skip trivial conversation flow markers ("I see", "let me check", etc.).
7. Predicate is descriptive verb phrase — natural language, NOT snake_case.

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

_SYSTEM_TEXT_EXTRACT_FREE = """\
Extract factual triples (subject, predicate, object) from the ASSISTANT RESPONSE (text).
Base each fact on information present or clearly implied in the text.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 4 facts. If fewer clear facts exist, return only what exists.
2. Each evidence MUST be a sentence ending in period.
3. Self-contained: Resolve pronouns and implicit references.
4. No duplicates: Same fact extracted once only.
5. No fabrication: Only extract what is present or clearly implied.
6. If uncertain, include "confidence": 0.5-0.9 in qualifiers.
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

_SYSTEM_REFINEMENT_FREE = """\
You are a fact extraction REFINER. Facts were already extracted from the source text.
Find MISSED factual triples that are in the source but NOT in the existing list.

Output ONLY valid JSON. No extra text.

RULES:
1. Max 2 new facts. If nothing missed, return empty list.
2. HIGH PRECISION: Only extract facts EXPLICITLY in the source.
3. Do NOT repeat facts from the existing list.
4. Each evidence MUST be a sentence ending in period.
5. Self-contained: Resolve pronouns and implicit references.
6. Predicate is descriptive verb phrase — natural language, NOT snake_case.

Output format:
{
  "extractions": [
    {
      "evidence": "<sentence ending with .>",
      "category": "code|decision|explanation|requirement|other",
      "subject": "<entity name>",
      "predicate": "<descriptive verb phrase>",
      "object": "<value>",
      "qualifiers": {}
    }
  ]
}

If nothing missed: {"extractions": []}."""


# ── Chunking utility ──────────────────────────────────────────


def _split_atomic(text: str, max_chars: int = 400) -> list[str]:
    """Split text into ~max_chars chunks at sentence/paragraph boundaries."""
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


# ── EDC-style predicate canonicalization helpers ──────────────

_definition_cache_edc: dict[str, str] = {}
_DEFINITION_PROMPT_EDC = (
    "Define this predicate briefly — what relation does it express?\n\nPredicate: {}"
)


def _get_predicate_definition(raw_predicate: str) -> str:
    if not raw_predicate:
        return ""
    if raw_predicate in _definition_cache_edc:
        return _definition_cache_edc[raw_predicate]
    prompt = _DEFINITION_PROMPT_EDC.format(raw_predicate)
    try:
        import urllib.request as _ur

        body = json.dumps(
            {"model": "test", "messages": [{"role": "user", "content": prompt}]}
        ).encode()
        req = _ur.Request(
            "http://127.0.0.1:8082/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            definition = data["choices"][0]["message"]["content"].strip().strip("\"'")
            if definition and len(definition) > 5:
                _definition_cache_edc[raw_predicate] = definition
                return definition
    except Exception as e:
        print(f"    [def-gen] error '{raw_predicate[:40]}': {e}")
    return raw_predicate


def _snake_case(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


def _raw_pred(fact: dict) -> str:
    return fact.get("predicate_raw", fact.get("predicate", ""))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na * nb > 0 else 0.0


# ── EDC: LLM-as-judge (WorkStealer dual 8082+8083) ────────────

_llm_judge_stats_edc: dict[str, int] = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}
_embed_cache_edc: dict[str, list[float]] = {}


def _llm_judge(pred_a: str, pred_b: str) -> float:
    """WorkStealer LLM-as-judge: threading + queue, first response wins."""
    global _llm_judge_stats_edc
    prompt = (
        f"Do these two predicates mean the same thing?\n\n"
        f"A: '{pred_a}'\nB: '{pred_b}'\n\n"
        f"Answer ONLY: equivalent | different | uncertain"
    )
    import queue as _q
    import threading as _th

    q = _q.Queue()

    def _work(port):
        try:
            import urllib.request as _ur

            body = json.dumps(
                {"model": "test", "messages": [{"role": "user", "content": prompt}]}
            ).encode()
            req = _ur.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with _ur.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                raw = data["choices"][0]["message"]["content"].strip().lower()
                q.put(raw)
        except Exception:
            pass

    for p in (8082, 8083):
        _th.Thread(target=_work, args=(p,), daemon=True).start()
    try:
        raw = q.get(timeout=20)
    except _q.Empty:
        _llm_judge_stats_edc["uncertain"] += 1
        return 0.5  # uncertain
    _llm_judge_stats_edc["calls"] += 1
    if "equivalent" in raw:
        _llm_judge_stats_edc["merged"] += 1
        return 0.85
    if "different" in raw:
        _llm_judge_stats_edc["split"] += 1
        return 0.0
    _llm_judge_stats_edc["uncertain"] += 1
    return 0.5


def _embed_text_8081(text: str) -> Optional[list[float]]:
    """Embed text via 4B embed on :8081. Returns None on failure."""
    import urllib.request as _ur

    body = json.dumps({"input": text, "model": "default"}).encode()
    try:
        req = _ur.Request(
            "http://127.0.0.1:8081/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with _ur.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data["data"][0]["embedding"]
    except Exception as e:
        print(f"    [embed] embedding failed: {e}", flush=True)
        return None


def _cached_embed_edc(text: str) -> Optional[list[float]]:
    if not text:
        return None
    if text in _embed_cache_edc:
        return _embed_cache_edc[text]
    vec = _embed_text_8081(text)
    if vec:
        _embed_cache_edc[text] = vec
    return vec


def _group_predicates(facts: list[dict]) -> list[dict]:
    """Group similar predicates via 3-tier: SeqMatcher → Embedding → LLM-as-judge.

    Tier 1: SequenceMatcher 0.85 blocking (non-transitive)
    Tier 2: Embedding definition cosine (0.85 merge / 0.65-0.85 LLM / <0.65 split)
    Tier 3: LLM-as-judge for all 0.65-0.85 pairs (no margin skip)

    Falls back gracefully when embed server (:8081) is unavailable.
    """
    if not facts:
        return facts

    from difflib import SequenceMatcher

    global _llm_judge_stats_edc
    _llm_judge_stats_edc = {"calls": 0, "merged": 0, "split": 0, "uncertain": 0}

    # Stage 1: Non-transitive SequenceMatcher grouping
    groups = []
    for i, fa in enumerate(facts):
        pa = _snake_case(_raw_pred(fa))
        matched = False
        for g in groups:
            rep_i = min(g)
            pb = _snake_case(_raw_pred(facts[rep_i]))
            if SequenceMatcher(None, pa, pb).ratio() >= 0.85:
                g.add(i)
                matched = True
                break
        if not matched:
            groups.append({i})

    # Stage 2+3: Embedding 3-tier → LLM-as-judge for ambiguous
    rep_map: dict[int, tuple[int, set]] = {}
    for g in groups:
        if not g:
            continue
        members = [facts[i] for i in g]
        raw_counts: dict[str, int] = {}
        for m in members:
            raw = _raw_pred(m)
            raw_counts[raw] = raw_counts.get(raw, 0) + 1
        best = max(raw_counts, key=raw_counts.get)
        rep_idx = next(i for i in g if _raw_pred(facts[i]) == best)
        rep_map[id(g)] = (rep_idx, g)

    rep_ids = list(rep_map.keys())
    if len(rep_ids) >= 2:
        # Try embed — if :8081 unavailable, skip to LLM-as-judge for all pairs
        embed_ok = False
        embed_count = 0
        test_vec = _embed_text_8081("test")
        if test_vec:
            embed_ok = True
            print(f"    [embed] checking {len(rep_ids)} group representatives...")

        merged_group_ids: set[int] = set()
        for i in range(len(rep_ids)):
            if rep_ids[i] in merged_group_ids:
                continue
            ri, gi = rep_map[rep_ids[i]]
            fi = facts[ri]
            if embed_ok:
                ei = _cached_embed_edc(_get_predicate_definition(_raw_pred(fi)))
                if ei is None:
                    continue
                embed_count += 1
            for j in range(i + 1, len(rep_ids)):
                if rep_ids[j] in merged_group_ids:
                    continue
                rj, gj = rep_map[rep_ids[j]]
                fj = facts[rj]

                if embed_ok:
                    ej = _cached_embed_edc(_get_predicate_definition(_raw_pred(fj)))
                    if ej is None:
                        continue
                    embed_count += 1
                    sim = _cosine_similarity(ei, ej)
                else:
                    sim = 0.70  # force all pairs through LLM-as-judge

                if embed_ok and sim >= 0.85:
                    gi |= gj
                    merged_group_ids.add(rep_ids[j])
                    print(
                        f"    [def-embed] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (cos={sim:.3f}, tier-1)"
                    )
                elif sim >= 0.65:
                    verdict = _llm_judge(_raw_pred(fi), _raw_pred(fj))
                    if verdict == 0.85:
                        gi |= gj
                        merged_group_ids.add(rep_ids[j])
                        print(
                            f"    [judge] merged '{_raw_pred(fi)}' -> '{_raw_pred(fj)}' (LLM: equivalent)"
                        )
                    elif verdict == 0.0:
                        print(
                            f"    [judge] split '{_raw_pred(fi)}' != '{_raw_pred(fj)}' (LLM: different)"
                        )
                    else:
                        print(
                            f"    [judge] uncertain '{_raw_pred(fi)}' vs '{_raw_pred(fj)}' (LLM uncertain, keeping separate)"
                        )

    final_groups = [g for g in groups if id(g) not in merged_group_ids]
    merged_count = sum(1 for g in final_groups if len(g) > 1)
    if embed_ok:
        tier = "3-tier" if embed_count >= 2 else "0-tier (all definitions failed)"
        print(f"    [embed] {tier}: {merged_count} groups formed (embed+judge)")
    elif merged_count:
        print(f"    [embed] {merged_count} groups formed (judge)")
    if _llm_judge_stats_edc["calls"] > 0:
        print(
            f"    [judge] calls={_llm_judge_stats_edc['calls']} "
            f"merged={_llm_judge_stats_edc['merged']} split={_llm_judge_stats_edc['split']} "
            f"uncertain={_llm_judge_stats_edc['uncertain']}"
        )

    # Assign canonical
    for group in final_groups:
        members = [facts[i] for i in group]
        rc = {}
        for m in members:
            r = _raw_pred(m)
            rc[r] = rc.get(r, 0) + 1
        canonical = max(rc, key=rc.get)
        canonical_snake = _snake_case(canonical)
        for idx in group:
            facts[idx]["predicate"] = canonical_snake
            facts[idx]["predicate_group"] = canonical
    return facts


def _normalize_predicate(fact: dict) -> dict:
    """Normalize predicate: store raw, write snake_case canonical form."""
    raw = fact.get("predicate", "")
    fact["predicate_raw"] = raw
    fact["predicate"] = _snake_case(raw)
    return fact


def _dedup_post_norm(facts: list[dict]) -> list[dict]:
    """Re-dedup after normalization: collapse (subject, normalized_pred, object)."""
    seen = {}
    for f in facts:
        key = (f.get("subject", ""), f.get("predicate", ""), f.get("object", ""))
        conf = f.get("qualifiers", {}).get("confidence", 1.0)
        if key not in seen or conf > seen[key].get("qualifiers", {}).get("confidence", 0):
            seen[key] = f
    result = list(seen.values())
    result.sort(key=lambda f: f.get("qualifiers", {}).get("confidence", 1.0), reverse=True)
    return result


def _normalize_freeform_pipeline(facts: list[dict]) -> list[dict]:
    """Full EDC normalization pipeline: snake_case → group → dedup."""
    if not facts:
        return facts
    for f in facts:
        _normalize_predicate(f)
    raw_preds = sorted({_raw_pred(f) for f in facts})
    print(f"    Unique raw predicates ({len(raw_preds)}): {raw_preds}")
    before = len(facts)
    facts = _group_predicates(facts)
    norm_preds = sorted({f.get("predicate", "") for f in facts})
    print(f"    After grouping: {len(norm_preds)} unique predicates")
    after = len(facts)
    facts = _dedup_post_norm(facts)
    print(f"    Dedup: {before} -> {len(facts)} (group+dedup removed {before - len(facts)})")
    return facts


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

SYSTEM_DESCRIBE_FILE = """\
You are a file description agent for a developer server. Given a filename,
MIME type, and file content (or first 2 KB for text files), produce a one-line
description and keyword tags for search/discovery.

Output STRICT JSON:
{
  "description": "One-line summary of what this file contains (max 15 words)",
  "tags": ["tag1", "tag2", "tag3"]
}

Rules:
- description must be factual and based only on filename, type, and content
- tags: 2-5 relevant keywords for search (include file type, source, purpose)
- For binary/non-text files, describe based on filename and mime_type alone
- For text files, use the content sample to determine the topic
- If content is empty or unreadable, describe by filename and extension only"""

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


# ── JSON parser ─────────────────────────────────────────────────


def _parse_json(raw: str, label: str = "LLM", attempt: int = 1) -> Optional[Dict[str, Any]]:
    cleaned = strip_think(raw)
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(
            raw, stage=f"extract_{label}", error="parse_llm_json returned None", attempt=attempt
        )
    return result


# Backward compat aliases for test files
SYSTEM_DAY_EXTRACT = _SYSTEM_TEXT_EXTRACT


def _extract_section(
    section_type: str, source_text: str, pulse_context: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    if not source_text:
        return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
    return _extract_single(section_type, source_text, pulse_context=pulse_context)


def _calc_max_tokens(text_len: int) -> Optional[int]:
    """CAP 이내 실현 가능한 max_tokens로 역산. 부족 시 None 반환.

    ceil(300ch/1fact) 기반 wanted와 CAP 역산 achievable 중
    작은 쪽 선택 → 절대 캡 초과 요청 불가.
    TIMEOUT_PER_TOK=1.2는 solo section-major(동시성=1) 기준.
    """
    extra = math.ceil(text_len / 300) * TOKENS_PER_300CH
    wanted = MAX_TOKENS_BASE + extra

    overhead = TIMEOUT_BASE + int(text_len * TIMEOUT_PER_CHAR) + GEN_TIME_BUF
    if overhead >= CAP:
        print(
            json.dumps(
                {
                    "event": "max_tokens_overflow",
                    "text_len": text_len,
                    "overhead_sec": overhead,
                    "cap_sec": CAP,
                    "reason": "prefill_exceeds_cap",
                }
            ),
            flush=True,
        )
        return None  # prefill만으로 CAP 초과
    achievable = int((CAP - overhead) / TIMEOUT_PER_TOK)
    if achievable < MIN_USEFUL_TOKENS:
        print(
            json.dumps(
                {
                    "event": "max_tokens_overflow",
                    "text_len": text_len,
                    "achievable": achievable,
                    "min_useful": MIN_USEFUL_TOKENS,
                    "reason": "below_min_useful",
                }
            ),
            flush=True,
        )
        return None  # 생성 가능 token이 너무 적음 → 명시적 거절

    final = min(wanted, achievable)
    if final < wanted:
        print(
            json.dumps(
                {
                    "event": "tokens_truncated",
                    "wanted": wanted,
                    "achievable": achievable,
                    "text_len": text_len,
                }
            ),
            flush=True,
        )
    return final


def _calc_timeout(total_chars: int, max_tokens: int) -> int:
    est = (
        TIMEOUT_BASE
        + int(total_chars * TIMEOUT_PER_CHAR)
        + int(max_tokens * TIMEOUT_PER_TOK)
        + GEN_TIME_BUF
    )
    return min(est, CAP)


# ── Low-value evidence filter ───────────────────────────────────

_LOW_VALUE_PATTERNS = re.compile(
    r"^(the user (asked|said|wrote|mentioned|replied|requested|is asking|is trying|wants|needs|"
    r"started|continued|responded|confirmed|indicated|provided|gave|"
    r"is working|is looking|is suggesting|is proposing|is discussing|"
    r"has been working|has been using|has asked|has reported))"
    r"|^(the assistant (explained|answered|provided|responded|gave|said|wrote|mentioned|is explaining|is providing))"
    r"|^(this (is a|is an|involves|refers to|seems to be|appears to be))"
    r"|^(there is (a |an |the |some |a discussion ))",
    re.IGNORECASE,
)


def _is_low_value(evidence: str) -> bool:
    stripped = evidence.strip()
    if len(stripped) < 25:
        return True
    return bool(_LOW_VALUE_PATTERNS.search(stripped))


# ── Entity context loader ───────────────────────────────────────


def _load_entity_context(turn_id: str) -> Optional[str]:
    if not turn_id:
        return None
    sql = (
        "SELECT evidence::text FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        "AND fact_type = 'entity_scan' "
        "ORDER BY fact_index DESC LIMIT 1"
    )
    rows = psql_json(sql)
    if not rows:
        return None
    try:
        data = json.loads(rows[0].get("evidence", "{}"))
    except (json.JSONDecodeError, KeyError):
        return None
    files = data.get("files", [])
    functions = data.get("functions", [])
    classes = data.get("classes", [])
    libraries = data.get("libraries", [])
    models_list = data.get("models", [])
    variables = data.get("variables", [])
    services = data.get("services", [])

    # Backward compat: old format only has files+functions
    all_found = bool(
        files or functions or classes or libraries or models_list or variables or services
    )
    if not all_found:
        return None

    parts = [
        "=== DATA: entity START ===",
        "The following entities were detected in this turn via pattern matching.",
        "",
    ]
    if files:
        parts.append("Files referenced: " + ", ".join(sorted(files)))
    if functions:
        parts.append("Functions referenced: " + ", ".join(sorted(functions)))
    if classes:
        parts.append("Classes referenced: " + ", ".join(sorted(classes)))
    if libraries:
        parts.append("Libraries referenced: " + ", ".join(sorted(libraries)))
    if models_list:
        parts.append("Models referenced: " + ", ".join(sorted(models_list)))
    if variables:
        parts.append("Constants referenced: " + ", ".join(sorted(variables)))
    if services:
        parts.append("Services referenced: " + ", ".join(sorted(services)))
    parts.append("")
    parts.append(
        "Use these as grounding references when extracting facts. "
        "If an extracted fact references one of these entities, it is more likely "
        "to be faithful to the source."
    )
    parts.append("=== DATA: entity END ===")
    return "\n".join(parts)


# ── Single section extraction ───────────────────────────────────


def _extract_single(
    section_type: str,
    source_text: str,
    pulse_context: Optional[str] = None,
    timeout: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    if not source_text:
        return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}

    prompt_map = {
        "user": _SYSTEM_USER_EXTRACT,
        "thinking": _SYSTEM_THINKING_EXTRACT,
        "text": _SYSTEM_TEXT_EXTRACT,
    }
    system_prompt = prompt_map.get(section_type, _SYSTEM_TEXT_EXTRACT)
    if pulse_context:
        system_prompt = f"{pulse_context}\n\n{system_prompt}"

    if timeout is None:
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            print(
                json.dumps(
                    {
                        "event": "extract_skip_overflow",
                        "text_len": len(source_text),
                        "max_input_advertised": MAX_INPUT_CHARS_ADVERTISED,
                    }
                ),
                flush=True,
            )
            return None
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)

    meta = _call_with_8082_retry(
        call_llm,
        [{"role": "system", "content": system_prompt}, {"role": "user", "content": source_text}],
        model="day_extract",
        max_tokens=max_tok,
        temperature=TEMP_EXTRACT,
        timeout=timeout,
        json_mode=True,
        return_meta=True,
    )
    raw = meta["content"]
    parsed = _parse_json(raw, f"day_extract_{section_type}")
    if parsed is None:
        return None
    ex = parsed.get("extractions", [])
    if not isinstance(ex, list):
        return None
    skip = parsed.get("skip_verdict") == "skip"
    if skip:
        result = {
            "extractions": [],
            "skip": True,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }
        _observe_extract_usage(
            section_type, len(source_text), max_tok, meta["usage"], meta["elapsed_ms"]
        )
        return result
    for e in ex:
        e["fact_type"] = section_type
    # Drop incomplete evidence (must end with sentence-ending punctuation)
    before = len(ex)
    ex = [e for e in ex if e.get("evidence", "").rstrip().endswith((".", "!", "?"))]
    if before != len(ex):
        print(f"  [_extract_single] dropped {before - len(ex)} incomplete evidence(s)", flush=True)
    # Drop low-value evidence
    before_lv = len(ex)
    ex = [e for e in ex if not _is_low_value(e.get("evidence", ""))]
    if before_lv != len(ex):
        print(
            f"  [_extract_single] dropped {before_lv - len(ex)} low-value evidence(s)", flush=True
        )
    _observe_extract_usage(
        section_type, len(source_text), max_tok, meta["usage"], meta["elapsed_ms"]
    )
    return {
        "extractions": ex,
        "usage": meta["usage"],
        "timings": meta["timings"],
        "elapsed_ms": meta["elapsed_ms"],
    }


# ── Per-turn extraction (user → thinking → text) ────────────────


def _observe_extract_usage(
    section_type: str, text_len: int, max_tokens: int, meta_usage: Dict, elapsed_ms: int
) -> None:
    """Log extraction token usage to observations table."""
    try:
        from lib.observation import observe

        pt = meta_usage.get("prompt_tokens", 0) or 0
        ct = meta_usage.get("completion_tokens", 0) or 0
        ctx = {
            "section": section_type,
            "text_len": text_len,
            "max_tokens": max_tokens,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "elapsed_ms": elapsed_ms,
        }
        observe(
            f"extract: {section_type} prompt={pt} comp={ct} max={max_tokens}",
            category="usage",
            source="pipeline:extract_llm",
            context=ctx,
            tags={"domain": ["extract", "token_usage"]},
        )
    except Exception:
        pass


def _merge_usage(target: Dict[str, int], usage: Dict) -> None:
    if not usage:
        return
    for k in ("prompt_tokens", "completion_tokens"):
        v = usage.get(k, 0) or 0
        target[k] = (target.get(k, 0) or 0) + v


def _extract_for_turn(
    turn: dict, pulse_context: Optional[str] = None
) -> Tuple[dict, Optional[Dict[str, Any]], Optional[str]]:
    user_turn = turn.get("user_turn") or ""
    thinking = turn.get("thinking") or ""
    text = turn.get("text") or ""

    entity_context = _load_entity_context(turn.get("id", ""))

    def _robust_extract(section_type, source_text, pulse_context=None, max_attempts=2):
        for attempt in range(max_attempts):
            try:
                return _extract_section(section_type, source_text, pulse_context=pulse_context)
            except Exception as e:
                print(
                    f"      [{section_type}] attempt {attempt + 1}/{max_attempts} failed: {e}",
                    flush=True,
                )
                if attempt < max_attempts - 1:
                    _recover_8082()
                    time.sleep(6)
        return None

    all_extractions: List[Dict] = []
    total_usage: Dict[str, int] = {}
    total_elapsed_ms = 0.0

    any_skip = False
    if user_turn:
        t0 = time.monotonic()
        res = _robust_extract("user", user_turn)
        if res and res.get("skip"):
            any_skip = True
            print("      [user] noise skip", flush=True)
        elif res and res.get("extractions"):
            all_extractions.extend(res["extractions"])
            _merge_usage(total_usage, res.get("usage", {}))
            total_elapsed_ms += time.monotonic() - t0
            print(
                f"      [user] {len(res['extractions'])} facts ({time.monotonic() - t0:.0f}s)",
                flush=True,
            )
        elif res is None:
            print("      [user section] failed after retries", flush=True)
    time.sleep(6)

    # thinking section: NOT extracted — used as context for text extraction
    # F-CoT principle: reasoning trace is context only, not extraction target
    thinking_context = None
    if thinking and len(thinking.strip()) > 5:
        thinking_context = (
            "<<< REASONING: thinking START >>>\n"
            f"{context_limit(thinking)}\n"
            "<<< REASONING: thinking END >>>"
        )

    if text:
        t0 = time.monotonic()
        combined_context = entity_context
        if thinking_context:
            combined_context = (
                f"{thinking_context}\n\n{entity_context}" if entity_context else thinking_context
            )
        res = _robust_extract("text", text, pulse_context=combined_context)
        if res and res.get("skip"):
            any_skip = True
            print("      [text] noise skip", flush=True)
        elif res and res.get("extractions"):
            all_extractions.extend(res["extractions"])
            _merge_usage(total_usage, res.get("usage", {}))
            total_elapsed_ms += time.monotonic() - t0
            print(
                f"      [text] {len(res['extractions'])} facts ({time.monotonic() - t0:.0f}s)",
                flush=True,
            )
        elif res is None:
            print("      [text section] failed after retries", flush=True)

    if not all_extractions and any_skip:
        return (turn, None, "noise skip")
    if not all_extractions:
        return (turn, None, "all sections returned empty")

    return (
        turn,
        {
            "extractions": all_extractions,
            "usage": total_usage,
            "timings": {},
            "elapsed_ms": total_elapsed_ms,
        },
        None,
    )


# ── Section-major solo processing ───────────────────────────────


def _checkpoint_sections(extractions):
    return set(
        e.get("fact_type")
        for e in extractions
        if e.get("fact_type") in ("user", "thinking", "text")
    )


def _extract_dual_section_major(
    dual_turns: List[dict],
    pulse_context: Optional[str] = None,
    dry_run: bool = False,
) -> Generator[Tuple[dict, Optional[Dict], Optional[str]], None, None]:
    """Section-major dual 4B extraction: A-Strict (8082) + B-Xplore (8083) -> FactArbiter.

    Replaces _extract_solo_section_major. Same interface and yield contract.
    Each section (user, text) runs both models concurrently per turn via threading.
    Results consolidated via FactArbiter (pure Python SequenceMatcher dedup).
    """
    from difflib import SequenceMatcher

    from extract import _load_checkpoint, _save_checkpoint
    from lib.arbiter import FactArbiter, FactStatus, _norm

    arbiter = FactArbiter(threshold=0.95)

    _ENTITY_RESOLVER_4B = """\
You are an Entity Resolver. Determine if two entity names refer to the same thing.
Answer YES only if they clearly refer to the same real-world entity.
Answer NO if they are different entities.

Examples:
- "day-extractor" vs "day-extractor model" → YES
- "extract_llm.py" vs "extract.py" → NO
- "PostgreSQL" vs "Postgres" → YES
- "GPU memory" vs "CPU memory" → NO
- "threads=4" vs "4 threads" → YES

Return ONLY valid JSON:
{"same": "yes", "reason": "why (≤10 words)"}
or
{"same": "no", "reason": "why (≤10 words)"}"""

    # Entity context for text section (thinking as context per F-CoT)
    entity_map = {}
    for t in dual_turns:
        base_ctx = _load_entity_context(t["id"]) or ""
        thinking = (t.get("thinking") or "").strip()
        if thinking:
            tc = f"<<< REASONING: thinking START >>>\n{context_limit(thinking)}\n<<< REASONING: thinking END >>>"
            entity_map[t["id"]] = f"{tc}\n\n{base_ctx}" if base_ctx else tc
        else:
            entity_map[t["id"]] = base_ctx or None

    turn_data: Dict[str, dict] = {}
    for t in dual_turns:
        ckpt = _load_checkpoint(t["id"]) if not dry_run else None
        if ckpt:
            turn_data[t["id"]] = {"extractions": ckpt, "total_usage": {}, "skip": False}
        else:
            turn_data[t["id"]] = {"extractions": [], "total_usage": {}, "skip": False}

    t0 = time.monotonic()

    def _xplore_single(
        section_type: str, source_text: str, ctx: Optional[str] = None
    ) -> Optional[Dict]:
        if not source_text:
            return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
        prompt_map = {
            "user": _SYSTEM_USER_EXTRACT_XPLORE,
            "thinking": _SYSTEM_THINKING_EXTRACT,
            "text": _SYSTEM_TEXT_EXTRACT_XPLORE,
        }
        system_prompt = prompt_map.get(section_type, _SYSTEM_TEXT_EXTRACT_XPLORE)
        if ctx:
            system_prompt = f"{ctx}\n\n{system_prompt}"
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            return None
        # 4B model needs more tokens for reasoning overhead — double
        max_tok = min(4096, max_tok * 2)
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)
        try:
            meta = _call_with_8083_retry(
                call_llm,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": source_text},
                ],
                model="day_extract_b",
                max_tokens=max_tok,
                temperature=TEMP_EXTRACT,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
            )
        except Exception as e:
            print(f"  [B-Xplore] call failed: {e}", flush=True)
            return None
        raw = meta["content"]
        parsed = _parse_json(raw, f"xplore_{section_type}")
        if parsed is None:
            return None
        ex = parsed.get("extractions", [])
        if not isinstance(ex, list):
            return None
        for e in ex:
            e["fact_type"] = section_type
        # Normalize evidence punctuation (same as _strict_single)
        before = len(ex)
        fixed = 0
        cleaned = []
        for e in ex:
            ev = (e.get("evidence") or "").strip()
            if not ev:
                continue
            if not ev.endswith((".", "!", "?")):
                if len(ev) < 20:
                    continue
                e["evidence"] = ev + "."
                fixed += 1
            cleaned.append(e)
        ex = cleaned
        if before != len(ex) or fixed:
            print(f"    [B-Xplore] dropped {before - len(ex)}, fixed {fixed}", flush=True)
        return {
            "extractions": ex,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }

    def _strict_single(
        section_type: str, source_text: str, ctx: Optional[str] = None
    ) -> Optional[Dict]:
        if not source_text:
            return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
        prompt_map = {
            "user": _SYSTEM_USER_EXTRACT_STRICT,
            "thinking": _SYSTEM_THINKING_EXTRACT,
            "text": _SYSTEM_TEXT_EXTRACT_STRICT,
        }
        system_prompt = prompt_map.get(section_type, _SYSTEM_TEXT_EXTRACT_STRICT)
        if ctx:
            system_prompt = f"{ctx}\n\n{system_prompt}"
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            return None
        # 4B model needs more tokens for reasoning overhead — double
        max_tok = min(4096, max_tok * 2)
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)
        try:
            meta = _call_with_8082_retry(
                call_llm,
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": source_text},
                ],
                model="day_extract",
                max_tokens=max_tok,
                temperature=TEMP_EXTRACT,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
            )
        except Exception as e:
            print(f"  [A-Strict] call failed: {e}", flush=True)
            return None
        raw = meta["content"]
        parsed = _parse_json(raw, f"strict_{section_type}")
        if parsed is None:
            return None
        ex = parsed.get("extractions", [])
        if not isinstance(ex, list):
            return None
        skip = parsed.get("skip_verdict") == "skip"
        for e in ex:
            e["fact_type"] = section_type
        if skip:
            return {
                "extractions": [],
                "skip": True,
                "usage": meta["usage"],
                "timings": meta["timings"],
                "elapsed_ms": meta["elapsed_ms"],
            }
        # Normalize evidence punctuation:
        # - Empty evidence → drop
        # - <20 chars without punctuation → drop (likely fragment)
        # - ≥20 chars without punctuation → auto-append period (4B model quirk)
        before = len(ex)
        fixed = 0
        cleaned = []
        for e in ex:
            ev = (e.get("evidence") or "").strip()
            if not ev:
                continue
            if not ev.endswith((".", "!", "?")):
                if len(ev) < 20:
                    continue  # too short for auto-fix, likely fragment
                e["evidence"] = ev + "."
                fixed += 1
            cleaned.append(e)
        ex = cleaned
        if before != len(ex) or fixed:
            print(f"    [A-Strict] dropped {before - len(ex)}, fixed {fixed}", flush=True)
        return {
            "extractions": ex,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }

    def _dual_extract_section(section_type: str, source_getter) -> int:
        targets = [
            t
            for t in dual_turns
            if source_getter(t)
            and not any(
                e.get("fact_type") == section_type for e in turn_data[t["id"]]["extractions"]
            )
        ]
        if not targets:
            return 0
        if dry_run:
            for t in targets:
                print(f"  [{section_type}] {t['id'][:8]} (dry-run skip)", flush=True)
            return 0
        print(f"  [dual] {section_type}: {len(targets)} turns (A:8082 + B:8083)", flush=True)
        count = 0
        for t in targets:
            src = source_getter(t)
            ctx = entity_map.get(t["id"]) if section_type == "text" else None
            a_res = None
            b_res = None

            def _a():
                nonlocal a_res
                try:
                    a_res = _strict_single(section_type, src, ctx=ctx)
                except Exception as e:
                    print(f"    [A-Strict] {t['id'][:8]} failed: {e}", flush=True)

            def _b():
                nonlocal b_res
                try:
                    b_res = _xplore_single(section_type, src, ctx=ctx)
                except Exception as e:
                    print(f"    [B-Xplore] {t['id'][:8]} failed: {e}", flush=True)

            th_a = threading.Thread(target=_a)
            th_b = threading.Thread(target=_b)
            th_a.start()
            th_b.start()
            th_a.join()
            th_b.join()
            a_facts = (a_res or {}).get("extractions", [])
            b_facts = (b_res or {}).get("extractions", [])
            _merge_usage(turn_data[t["id"]]["total_usage"], (a_res or {}).get("usage", {}))
            _merge_usage(turn_data[t["id"]]["total_usage"], (b_res or {}).get("usage", {}))
            if not a_facts and not b_facts:
                if a_res and a_res.get("skip"):
                    turn_data[t["id"]]["skip"] = True
                print(f"    [{t['id'][:8]}] both returned empty", flush=True)
                continue
            refs = arbiter.consolidate(a_facts, b_facts)
            # LLM resolve CONFLICT refs via dual 4B (A:8082 + B:8083, round-robin)
            conflict_refs = [ref for ref in refs if ref.status.name == "CONFLICT"]
            _rr_lock = threading.Lock()
            _rr = [0]

            def _do_resolve(ref):
                b_match = next(
                    (
                        fb
                        for fb in b_facts
                        if SequenceMatcher(
                            None,
                            _norm(ref.fact.get("subject", ""))
                            + " "
                            + _norm(ref.fact.get("predicate", "")),
                            _norm(fb.get("subject", "")) + " " + _norm(fb.get("predicate", "")),
                        ).ratio()
                        >= 0.80
                    ),
                    None,
                )
                if not b_match:
                    return
                b_ev = b_match.get("evidence", "") or ""
                msg = (
                    f"Fact A: [{ref.fact.get('subject', '?')}] {ref.fact.get('predicate', '?')} = {ref.fact.get('object', '?')}\n"
                    f"Fact B: [{b_match.get('subject', '?')}] {b_match.get('predicate', '?')} = {b_match.get('object', '?')}"
                )
                if b_ev:
                    msg += f"\nEvidence B: {b_ev[:300]}"
                with _rr_lock:
                    rr = _rr[0]
                    _rr[0] += 1
                fn = _call_with_8082_retry if rr % 2 == 0 else _call_with_8083_retry
                model = "day_extract" if rr % 2 == 0 else "day_extract_b"
                try:
                    meta = fn(
                        call_llm,
                        [
                            {"role": "system", "content": _CONFLICT_RESOLVER_4B},
                            {"role": "user", "content": msg},
                        ],
                        model=model,
                        max_tokens=128,
                        temperature=0.0,
                        timeout=30,
                        json_mode=True,
                        return_meta=True,
                    )
                except Exception as e:
                    print(f"  [conflict-detect] LLM call failed: {e}", flush=True)
                    return
                raw = meta["content"]
                verdict = _parse_json(raw, "conflict")
                if verdict is None:
                    return
                v = verdict.get("verdict")
                if v == "CONSENSUS":
                    ref.status = FactStatus.CONSENSUS
                    ref.fact = dict(b_match)
                elif v == "DIFFERENT_ASPECT":
                    ref.status = FactStatus.UNIQUE_A

            ths = []
            for ref in conflict_refs:
                t = threading.Thread(target=_do_resolve, args=(ref,))
                t.start()
                ths.append(t)
            for t in ths:
                t.join()
            merged = []
            for ref in refs:
                fact = dict(ref.fact)
                fact["fact_type"] = section_type
                fact["_source"] = ref.status.value
                merged.append(fact)

            turn_data[t["id"]]["extractions"].extend(merged)
            count += 1
            print(
                f"    [{t['id'][:8]}] A={len(a_facts)} B={len(b_facts)} => {len(merged)} merged",
                flush=True,
            )
            if not dry_run:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
        return count

    _dual_extract_section("user", lambda t: t.get("user_turn", "") or "")
    heartbeat("day_extract", "dual user done")
    time.sleep(6)

    _dual_extract_section("text", lambda t: t.get("text") or "")
    heartbeat("day_extract", f"dual text done, total={time.monotonic() - t0:.0f}s")

    # ── Cross-section dedup: normalize entities across user + text sections ──
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if len(all_facts) < 2:
            continue
        before = len(all_facts)

        # A: Exact dedup — same (subject, predicate, object) triples
        seen = set()
        deduped = []
        for f in all_facts:
            key = (
                f.get("subject", "").strip().lower(),
                f.get("predicate", "").strip().lower(),
                f.get("object", "").strip().lower(),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        all_facts = deduped

        # B: Subject normalization — dual 4B round-robin
        subjects = list(dict.fromkeys(f.get("subject", "") for f in all_facts))
        subj_map = {}
        _entity_rr = 0  # round-robin counter
        for i, s1 in enumerate(subjects):
            for s2 in subjects[i + 1 :]:
                if s1 in subj_map or s2 in subj_map:
                    continue
                ratio = SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()
                if ratio >= 0.95:
                    canonical = s1 if len(s1) <= len(s2) else s2
                    subj_map[s1] = canonical
                    subj_map[s2] = canonical
                elif ratio >= 0.70:
                    rr = _entity_rr % 2
                    _entity_rr += 1
                    fn = _call_with_8082_retry if rr == 0 else _call_with_8083_retry
                    model = "day_extract" if rr == 0 else "day_extract_b"
                    try:
                        meta = fn(
                            call_llm,
                            [
                                {"role": "system", "content": _ENTITY_RESOLVER_4B},
                                {"role": "user", "content": f'Entity A: "{s1}"\nEntity B: "{s2}"'},
                            ],
                            model=model,
                            max_tokens=16,
                            temperature=0.0,
                            timeout=15,
                            json_mode=True,
                            return_meta=True,
                        )
                        verdict = _parse_json(meta["content"], "entity_resolve")
                        if verdict and str(verdict.get("same", "")).lower() == "yes":
                            canonical = s1 if len(s1) <= len(s2) else s2
                            subj_map[s1] = canonical
                            subj_map[s2] = canonical
                    except Exception as e:
                        print(f"  [entity-resolve] LLM call failed: {e}", flush=True)
        if subj_map:
            for f in all_facts:
                old_subj = f.get("subject", "")
                canonical = subj_map.get(old_subj)
                if canonical:
                    f["subject"] = canonical
            seen2 = set()
            deduped2 = []
            for f in all_facts:
                key = (
                    f.get("subject", "").strip().lower(),
                    f.get("predicate", "").strip().lower(),
                    f.get("object", "").strip().lower(),
                )
                if key not in seen2:
                    seen2.add(key)
                    deduped2.append(f)
            all_facts = deduped2

        changed = len(all_facts) != before or bool(subj_map)
        if changed:
            print(
                f"    [{t['id'][:8]}] cross-section dedup/subj: {before} -> {len(all_facts)} (subj_map={len(subj_map)})",
                flush=True,
            )
            turn_data[t["id"]]["extractions"] = all_facts

    for t in dual_turns:
        td = turn_data[t["id"]]
        if td.get("skip") and not td["extractions"]:
            yield t, None, "noise skip"
        elif not td["extractions"]:
            yield t, None, "all sections returned empty"
        else:
            yield (
                t,
                {
                    "extractions": td["extractions"],
                    "usage": td["total_usage"],
                    "timings": {},
                    "elapsed_ms": 0,
                },
                None,
            )


_backward_compat = _extract_solo_section_major = _extract_dual_section_major


# ── Free-form extraction with Stop-and-Swap lazy embed ──────────


def _ensure_embed_8081() -> bool:
    """Start/ensure embed-4b on :8081 inside inference container without restart.

    Returns True if :8081 healthy. Non-blocking on existing healthy instance.
    """
    import urllib.request as _ur

    # Check if already healthy
    try:
        req = _ur.Request("http://127.0.0.1:8081/health")
        with _ur.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                return True
    except Exception:
        pass

    meta = MODEL_METADATA.get("embed-4b")
    if not meta:
        print("  [embed] FATAL: embed-4b not in MODEL_METADATA")
        return False

    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 2048)
    threads = meta.get("threads", 2)
    threads_batch = meta.get("threads_batch", 2)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        ["podman", "exec", "-d", "devforge-inference"]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--threads-batch",
            str(threads_batch),
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            "512",
            "--embedding",
            "--pooling",
            "last",
            "--embd-normalize",
            "-1",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "6",
            "--metrics",
        ]
    )

    print(f"  [embed] launching {model_file} on :{port} via podman exec...")
    import subprocess as _sp

    r = _sp.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [embed] launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False

    from lib.pod_manager import wait_health as _wh

    ok = _wh(port, timeout=120)
    if ok:
        print(f"  [embed] :{port} healthy with {model_file}")
    else:
        print(f"  [embed] :{port} health timeout")
    return ok


def _stop_embed_8081() -> None:
    """Kill embed-4b llama-server on :8081."""
    import subprocess as _sp

    _sp.run(
        ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8081"],
        timeout=10,
        capture_output=True,
    )


def _stop_extract_b_8083() -> None:
    """Kill extract-b llama-server on :8083."""
    import subprocess as _sp

    _sp.run(
        ["podman", "exec", "devforge-inference", "pkill", "-f", "llama-server.*8083[^0-9]"],
        timeout=10,
        capture_output=True,
    )


def _start_extract_b_8083() -> bool:
    """Launch day-extract-b (:8083) inside inference container."""
    from lib.pod_manager import wait_health as _wh

    meta = MODEL_METADATA.get("day-extractor-b")
    if not meta:
        print("  [extract-b] FATAL: day-extractor-b not in MODEL_METADATA")
        return False
    port = meta["port"]
    model_file = meta["file"]
    ctx = meta.get("ctx", 8192)
    threads = meta.get("threads", 4)
    parallel = meta.get("parallel", 1)
    cpus = meta.get("cpus", "")

    launch_cmd = ["/app/llama-server"]
    if cpus:
        launch_cmd = ["taskset", "-c", cpus] + launch_cmd

    cmd = (
        ["podman", "exec", "-d", "devforge-inference"]
        + launch_cmd
        + [
            "-m",
            f"/models/{model_file}",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            "--ctx-size",
            str(ctx),
            "--parallel",
            str(parallel),
            "--threads",
            str(threads),
            "--temp",
            "0.1",
            "--flash-attn",
            "on",
            "--timeout",
            "28800",
            "--batch-size",
            "512",
            "--ubatch-size",
            "256",
            "--cont-batching",
            "--no-mmap",
            "-lv",
            "1",
            "--metrics",
        ]
    )

    print(f"  [extract-b] launching {model_file} on :{port} via podman exec...")
    import subprocess as _sp

    r = _sp.run(cmd, capture_output=True, timeout=30, text=True)
    if r.returncode != 0:
        print(f"  [extract-b] launch failed (rc={r.returncode}): {r.stderr.strip()[:200]}")
        return False
    ok = _wh(port, timeout=120)
    if ok:
        print(f"  [extract-b] :{port} healthy")
    else:
        print(f"  [extract-b] :{port} health timeout")
    return ok


def _extract_edcr_freeform(
    dual_turns: List[dict],
    pulse_context: Optional[str] = None,
    dry_run: bool = False,
) -> Generator[Tuple[dict, Optional[Dict], Optional[str]], None, None]:
    """Free-form extraction with OIE → Canonicalize → Refinement (EDC+R).

    Phase 1 (OIE): Dual 4B (8082+8083) → FactArbiter → LLM conflict resolve
    Phase 2 (Canonicalize): SeqMatcher → Embed 3-tier (8081) → LLM-judge → dedup
    Phase 3 (Refinement): FACT-style context rewrite → Xplore(8083) re-extract → merge → cap 6

    Key changes from _extract_dual_section_major:
    1. Free-form prompts (no predicate snake_case constraints — Taxonomy Trap fix)
    2. 400-char no-overlap sentence/paragraph chunking (max 4 facts per chunk)
    3. EDC-style definition embedding for predicate canonicalization
    4. Stop-and-Swap: kill 8083 → embed 8081 → normalize → restart 8083
    5. FACT-style Refinement: remove identified facts → Xplore re-extract → merge → cap 6
    """
    from difflib import SequenceMatcher

    from extract import _load_checkpoint, _save_checkpoint
    from lib.arbiter import FactArbiter, FactStatus, _norm

    arbiter = FactArbiter(threshold=0.95)

    _ENTITY_RESOLVER_4B = """\
You are an Entity Resolver. Determine if two entity names refer to the same thing.
Answer YES only if they clearly refer to the same real-world entity.
Answer NO if they are different entities.

Examples:
- "day-extractor" vs "day-extractor model" → YES
- "extract_llm.py" vs "extract.py" → NO
- "PostgreSQL" vs "Postgres" → YES
- "GPU memory" vs "CPU memory" → NO
- "threads=4" vs "4 threads" → YES

Return ONLY valid JSON:
{"same": "yes", "reason": "why (≤10 words)"}
or
{"same": "no", "reason": "why (≤10 words)"}"""

    # Section-major extraction with chunking
    def _strict_freeform(section_type: str, source_text: str) -> Optional[Dict]:
        if not source_text:
            return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
        prompt = _SYSTEM_USER_EXTRACT_FREE if section_type == "user" else _SYSTEM_TEXT_EXTRACT_FREE
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            return None
        max_tok = min(4096, max_tok * 2)
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)
        try:
            meta = _call_with_8082_retry(
                call_llm,
                [{"role": "system", "content": prompt}, {"role": "user", "content": source_text}],
                model="day_extract",
                max_tokens=max_tok,
                temperature=TEMP_EXTRACT,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
            )
        except Exception as e:
            print(f"  [A-Free] call failed: {e}", flush=True)
            return None
        raw = meta["content"]
        parsed = _parse_json(raw, f"free_strict_{section_type}")
        if parsed is None:
            return None
        ex = parsed.get("extractions", [])
        if not isinstance(ex, list):
            return None
        for e in ex:
            e["fact_type"] = section_type
        # Evidence normalization (same as _strict_single)
        before = len(ex)
        fixed = 0
        cleaned = []
        for e in ex:
            ev = (e.get("evidence") or "").strip()
            if not ev:
                continue
            if not ev.endswith((".", "!", "?")):
                if len(ev) < 5:
                    continue
                e["evidence"] = ev + "."
                fixed += 1
            cleaned.append(e)
        ex = cleaned
        if before != len(ex) or fixed:
            print(f"    [A-Free] dropped {before - len(ex)}, fixed {fixed}", flush=True)
        return {
            "extractions": ex,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }

    def _xplore_freeform(section_type: str, source_text: str) -> Optional[Dict]:
        if not source_text:
            return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
        prompt = (
            _SYSTEM_USER_EXTRACT_XPLORE_FREE
            if section_type == "user"
            else _SYSTEM_TEXT_EXTRACT_XPLORE_FREE
        )
        max_tok = _calc_max_tokens(len(source_text))
        if max_tok is None:
            return None
        max_tok = min(4096, max_tok * 2)
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)
        try:
            meta = _call_with_8083_retry(
                call_llm,
                [{"role": "system", "content": prompt}, {"role": "user", "content": source_text}],
                model="day_extract_b",
                max_tokens=max_tok,
                temperature=TEMP_EXTRACT,
                timeout=timeout,
                json_mode=True,
                return_meta=True,
            )
        except Exception as e:
            print(f"  [B-Free] call failed: {e}", flush=True)
            return None
        raw = meta["content"]
        parsed = _parse_json(raw, f"free_xplore_{section_type}")
        if parsed is None:
            return None
        ex = parsed.get("extractions", [])
        if not isinstance(ex, list):
            return None
        for e in ex:
            e["fact_type"] = section_type
        before = len(ex)
        fixed = 0
        cleaned = []
        for e in ex:
            ev = (e.get("evidence") or "").strip()
            if not ev:
                continue
            if not ev.endswith((".", "!", "?")):
                if len(ev) < 5:
                    continue
                e["evidence"] = ev + "."
                fixed += 1
            cleaned.append(e)
        ex = cleaned
        if before != len(ex) or fixed:
            print(f"    [B-Free] dropped {before - len(ex)}, fixed {fixed}", flush=True)
        return {
            "extractions": ex,
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
        }

    def _dual_extract_section(section_type: str, source_getter) -> int:
        targets = [
            t
            for t in dual_turns
            if source_getter(t)
            and (section_type != "user" or len(source_getter(t).strip()) >= 15)
            and not any(
                e.get("fact_type") == section_type for e in turn_data[t["id"]]["extractions"]
            )
        ]
        if not targets:
            return 0
        if dry_run:
            for t in targets:
                print(f"  [{section_type}] {t['id'][:8]} (dry-run skip)", flush=True)
            return 0
        print(f"  [free-form] {section_type}: {len(targets)} turns (A:8082 + B:8083)", flush=True)

        def _process_one_turn(t: dict) -> int:
            """Process one turn: chunk → A∥B → arbiter → resolve → checkpoint."""
            src = source_getter(t)
            if not src:
                return 0
            chunks = _split_atomic(src)
            a_all: List[Dict] = []
            b_all: List[Dict] = []

            # Per-chunk extraction — parallel A(8082) + B(8083)
            for ci, chunk in enumerate(chunks):
                _par = {}

                def _run_a():
                    _par["a"] = _strict_freeform(section_type, chunk)

                def _run_b():
                    _par["b"] = _xplore_freeform(section_type, chunk)

                ta = threading.Thread(target=_run_a, daemon=True)
                tb = threading.Thread(target=_run_b, daemon=True)
                ta.start()
                tb.start()
                ta.join()
                tb.join()
                a_res, b_res = _par.get("a"), _par.get("b")

                if a_res and a_res.get("extractions"):
                    a_all.extend(a_res["extractions"])
                    _merge_usage(turn_data[t["id"]]["total_usage"], a_res.get("usage", {}))
                if b_res and b_res.get("extractions"):
                    b_all.extend(b_res["extractions"])
                    _merge_usage(turn_data[t["id"]]["total_usage"], b_res.get("usage", {}))
                a_n = len(a_res.get("extractions", [])) if a_res else 0
                b_n = len(b_res.get("extractions", [])) if b_res else 0
                print(
                    f"      {section_type} ch{ci}/{len(chunks)}: A={a_n} B={b_n}",
                    flush=True,
                )

            if not a_all and not b_all:
                print(f"    [{t['id'][:8]}] both returned empty", flush=True)
                return 0

            # FactArbiter merge
            refs = arbiter.consolidate(a_all, b_all)
            conflict_refs = [ref for ref in refs if ref.status.name == "CONFLICT"]
            _rr_lock = threading.Lock()
            _rr = [0]

            def _do_resolve(ref):
                b_match = next(
                    (
                        fb
                        for fb in b_all
                        if SequenceMatcher(
                            None,
                            _norm(ref.fact.get("subject", ""))
                            + " "
                            + _norm(ref.fact.get("predicate", "")),
                            _norm(fb.get("subject", "")) + " " + _norm(fb.get("predicate", "")),
                        ).ratio()
                        >= 0.80
                    ),
                    None,
                )
                if not b_match:
                    return
                b_ev = b_match.get("evidence", "") or ""
                msg = (
                    f"Fact A: [{ref.fact.get('subject', '?')}] {ref.fact.get('predicate', '?')} = {ref.fact.get('object', '?')}\n"
                    f"Fact B: [{b_match.get('subject', '?')}] {b_match.get('predicate', '?')} = {b_match.get('object', '?')}"
                )
                if b_ev:
                    msg += f"\nEvidence B: {b_ev[:300]}"
                with _rr_lock:
                    rr = _rr[0]
                    _rr[0] += 1
                fn = _call_with_8082_retry if rr % 2 == 0 else _call_with_8083_retry
                model = "day_extract" if rr % 2 == 0 else "day_extract_b"
                try:
                    meta = fn(
                        call_llm,
                        [
                            {"role": "system", "content": _CONFLICT_RESOLVER_4B},
                            {"role": "user", "content": msg},
                        ],
                        model=model,
                        max_tokens=128,
                        temperature=0.0,
                        timeout=30,
                        json_mode=True,
                        return_meta=True,
                    )
                except Exception as e:
                    print(f"  [conflict-detect] LLM call failed: {e}", flush=True)
                    return
                raw = meta["content"]
                verdict = _parse_json(raw, "conflict")
                if verdict is None:
                    return
                v = verdict.get("verdict")
                if v == "CONSENSUS":
                    ref.status = FactStatus.CONSENSUS
                    ref.fact = dict(b_match)
                elif v == "DIFFERENT_ASPECT":
                    ref.status = FactStatus.UNIQUE_A

            ths = []
            for ref in conflict_refs:
                t = threading.Thread(target=_do_resolve, args=(ref,))
                t.start()
                ths.append(t)
            for t in ths:
                t.join()

            merged = []
            for ref in refs:
                fact = dict(ref.fact)
                fact["fact_type"] = section_type
                fact["_source"] = ref.status.value
                s = fact.get("subject", "").strip()
                p = fact.get("predicate", "").strip()
                o = fact.get("object", "").strip()
                if s and p and o:
                    merged.append(fact)

            turn_data[t["id"]]["extractions"].extend(merged)
            n_chunks = len(chunks)
            print(
                f"    [{t['id'][:8]}] A={len(a_all)} B={len(b_all)} => {len(merged)} merged ({n_chunks} chunks)",
                flush=True,
            )
            if not dry_run:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
            return 1

        from concurrent.futures import ThreadPoolExecutor

        n_w = min(4, len(targets))
        with ThreadPoolExecutor(max_workers=n_w) as exe:
            counts = list(exe.map(_process_one_turn, targets))
        return sum(counts)

    turn_data: Dict[str, dict] = {}
    for t in dual_turns:
        ckpt = _load_checkpoint(t["id"]) if not dry_run else None
        if ckpt:
            turn_data[t["id"]] = {"extractions": ckpt, "total_usage": {}, "skip": False}
        else:
            turn_data[t["id"]] = {"extractions": [], "total_usage": {}, "skip": False}

    t0 = time.monotonic()

    _dual_extract_section("user", lambda t: t.get("user_turn", "") or "")
    heartbeat("day_extract", "free user done")
    time.sleep(6)

    _dual_extract_section("text", lambda t: t.get("text") or "")
    heartbeat("day_extract", f"free text done, total={time.monotonic() - t0:.0f}s")

    # ── Cross-section dedup ──
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if len(all_facts) < 2:
            continue
        before = len(all_facts)
        # Exact dedup
        seen = set()
        deduped = []
        for f in all_facts:
            key = (
                f.get("subject", "").strip().lower(),
                f.get("predicate", "").strip().lower(),
                f.get("object", "").strip().lower(),
            )
            if key not in seen:
                seen.add(key)
                deduped.append(f)
        all_facts = deduped
        # Subject normalization — dual 4B round-robin
        subjects = list(dict.fromkeys(f.get("subject", "") for f in all_facts))
        subj_map = {}
        _entity_rr = 0
        for i, s1 in enumerate(subjects):
            for s2 in subjects[i + 1 :]:
                if s1 in subj_map or s2 in subj_map:
                    continue
                ratio = SequenceMatcher(None, s1.lower().strip(), s2.lower().strip()).ratio()
                if ratio >= 0.95:
                    canonical = s1 if len(s1) <= len(s2) else s2
                    subj_map[s1] = canonical
                    subj_map[s2] = canonical
                elif ratio >= 0.70:
                    rr = _entity_rr % 2
                    _entity_rr += 1
                    fn = _call_with_8082_retry if rr == 0 else _call_with_8083_retry
                    model = "day_extract" if rr == 0 else "day_extract_b"
                    try:
                        meta = fn(
                            call_llm,
                            [
                                {"role": "system", "content": _ENTITY_RESOLVER_4B},
                                {
                                    "role": "user",
                                    "content": f'Entity A: "{s1}"\nEntity B: "{s2}"',
                                },
                            ],
                            model=model,
                            max_tokens=16,
                            temperature=0.0,
                            timeout=15,
                            json_mode=True,
                            return_meta=True,
                        )
                        verdict = _parse_json(meta["content"], "entity_resolve")
                        if verdict and str(verdict.get("same", "")).lower() == "yes":
                            canonical = s1 if len(s1) <= len(s2) else s2
                            subj_map[s1] = canonical
                            subj_map[s2] = canonical
                    except Exception as e:
                        print(f"  [entity-resolve] LLM call failed: {e}", flush=True)
        if subj_map:
            for f in all_facts:
                old_subj = f.get("subject", "")
                canonical = subj_map.get(old_subj)
                if canonical:
                    f["subject"] = canonical
            seen2 = set()
            deduped2 = []
            for f in all_facts:
                key = (
                    f.get("subject", "").strip().lower(),
                    f.get("predicate", "").strip().lower(),
                    f.get("object", "").strip().lower(),
                )
                if key not in seen2:
                    seen2.add(key)
                    deduped2.append(f)
            all_facts = deduped2
        changed = len(all_facts) != before or bool(subj_map)
        if changed:
            print(
                f"    [{t['id'][:8]}] cross-section dedup/subj: {before} -> {len(all_facts)} (subj_map={len(subj_map)})",
                flush=True,
            )
            turn_data[t["id"]]["extractions"] = all_facts

    # ── Stop-and-Swap: EDC normalization pass ──
    all_fact_groups = {}
    for t in dual_turns:
        all_facts = turn_data[t["id"]]["extractions"]
        if all_facts:
            all_fact_groups[t["id"]] = all_facts

    if all_fact_groups:
        print("\n  [swap] Stopping extract-b (:8083)...", flush=True)
        _stop_extract_b_8083()
        time.sleep(2)

        print("  [swap] Starting embed on :8081...", flush=True)
        embed_ok = _ensure_embed_8081()
        time.sleep(1)

        if embed_ok:
            for tid, facts in all_fact_groups.items():
                before = len(facts)
                print(f"    -- Normalization ({tid[:8]}) --", flush=True)
                facts = _normalize_freeform_pipeline(facts)
                if tid in turn_data:
                    turn_data[tid]["extractions"] = facts

        print("  [swap] Stopping embed (:8081)...", flush=True)
        _stop_embed_8081()
    else:
        print("  [swap] No facts to normalize, skipping Stop-and-Swap", flush=True)

    # Restart 8083 only if more turns expected (not in section-major, but for safety)
    _start_extract_b_8083()

    # ── Phase 3: Refinement (FACT-style) — turn-level parallel ──
    print("\n  [Refinement] FACT-style: find missed facts via Xplore(8083)", flush=True)
    refine_turns = [t for t in dual_turns if len(turn_data[t["id"]].get("extractions", [])) < 6]
    if not refine_turns:
        print("  [Refinement] all turns at 6/6, skip", flush=True)
    else:
        print(f"  [Refinement] {len(refine_turns)} turns need refinement", flush=True)

        def _refine_one_turn(t: dict) -> None:
            td = turn_data[t["id"]]
            existing = td.get("extractions", [])
            before = len(existing)
            new_facts = []
            new_facts_lock = threading.Lock()
            source_pairs = [
                ("user", t.get("user_turn", "") or ""),
                ("text", t.get("text") or ""),
            ]
            refine_tasks = []
            for section_type, source in source_pairs:
                if not source:
                    continue
                chunks = _split_atomic(source)
                existing_str = (
                    "\n".join(
                        f"  [{f.get('subject', '?')}] {f.get('predicate', '?')} = {f.get('object', '?')}"
                        for f in existing
                    )
                    or "  (none)"
                )
                for ci, chunk in enumerate(chunks):
                    refine_tasks.append((section_type, ci, chunk, existing_str))

            def _do_refine(task):
                section_type, ci, chunk, existing_str = task
                prompt = (
                    f"=== SOURCE TEXT ===\n{chunk}\n\n"
                    f"=== EXISTING FACTS ===\n{existing_str}\n\n"
                    f"Find MISSED factual triples from the source text. "
                    f"Do NOT extract facts already listed in EXISTING FACTS."
                )

                max_tok = _calc_max_tokens(len(chunk) + len(existing_str))
                if max_tok is None:
                    return
                max_tok = min(1024, max_tok * 2)
                timeout = _calc_timeout(len(chunk), max_tokens=max_tok)

                try:
                    meta = _call_with_8083_retry(
                        call_llm,
                        [
                            {"role": "system", "content": _SYSTEM_REFINEMENT_FREE},
                            {"role": "user", "content": prompt},
                        ],
                        model="day_extract_b",
                        max_tokens=max_tok,
                        temperature=TEMP_EXTRACT,
                        timeout=timeout,
                        json_mode=True,
                        return_meta=True,
                    )
                except Exception as e:
                    print(f"      refine {section_type} ch{ci}: call failed: {e}", flush=True)
                    return

                parsed = _parse_json(meta["content"], f"refine_{section_type}")
                facts = parsed.get("extractions", []) if parsed else []
                if facts:
                    for f in facts:
                        f["fact_type"] = section_type
                    print(
                        f"      refine {t['id'][:8]} {section_type} ch{ci}: +{len(facts)} fact(s)",
                        flush=True,
                    )
                    with new_facts_lock:
                        new_facts.extend(facts)
                        _merge_usage(td["total_usage"], meta.get("usage", {}))

            ref_threads = [
                threading.Thread(target=_do_refine, args=(task,), daemon=True)
                for task in refine_tasks
            ]
            for th in ref_threads:
                th.start()
            for th in ref_threads:
                th.join()

            if new_facts:
                all_facts = existing + new_facts
                seen = set()
                deduped = []
                for f in all_facts:
                    key = (
                        f.get("subject", "").strip().lower(),
                        f.get("predicate", "").strip().lower(),
                        f.get("object", "").strip().lower(),
                    )
                    if key not in seen:
                        seen.add(key)
                        deduped.append(f)
                all_facts = deduped
                if len(all_facts) > 6:
                    all_facts = sorted(
                        all_facts,
                        key=lambda f: f.get("qualifiers", {}).get("confidence", 1.0),
                        reverse=True,
                    )[:6]
                td["extractions"] = all_facts
                print(
                    f"    [{t['id'][:8]}] Refinement found {len(new_facts)} missed facts -> {len(all_facts)}/6 (was {before})",
                    flush=True,
                )
            else:
                print(f"    [{t['id'][:8]}] Refinement: no missed facts found", flush=True)

        from concurrent.futures import ThreadPoolExecutor

        n_ref = min(4, len(refine_turns))
        with ThreadPoolExecutor(max_workers=n_ref) as exe:
            exe.map(_refine_one_turn, refine_turns)

    for t in dual_turns:
        td = turn_data[t["id"]]
        if td.get("skip") and not td["extractions"]:
            yield t, None, "noise skip"
        elif not td["extractions"]:
            yield t, None, "all sections returned empty"
        else:
            yield (
                t,
                {
                    "extractions": td["extractions"],
                    "usage": td["total_usage"],
                    "timings": {},
                    "elapsed_ms": 0,
                },
                None,
            )
