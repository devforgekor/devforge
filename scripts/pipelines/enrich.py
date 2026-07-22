#!/usr/bin/env python3
# Status: experimental
# Path: day_cycle.py — MCP metadata enrichment (runs after extract)
"""Enrich Pipeline — post-extract MCP metadata enrichment (tldr, intent, entities, tags).

Runs AFTER extract pipeline in day_cycle.py to generate enrichment metadata
(tldr, intent, entities, tags) from already-extracted facts. This feeds the
verify stage and later the embedding layer.

Entity verification chain:
  1st: Substring match against source text (fast deterministic)
  2nd: Reranker relevance check (inference :8080) — GROUNDED/AMBIGUOUS→keep, UNGROUNDED→drop

State-based: NOT EXISTS enrich_meta is the sole filter. Checkpoint not needed.

Usage:
  python3 scripts/pipelines/enrich.py                    # batch
  python3 scripts/pipelines/enrich.py --turn-id <uuid>   # single turn (debug)
  python3 scripts/pipelines/enrich.py --limit 20         # batch cap
  python3 scripts/pipelines/enrich.py --dry-run          # simulate, no writes
"""

import difflib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import context_limit, strip_think
from lib.db import esc_sql, psql, psql_json, psql_ok
from lib.enrich_few_shot import load_few_shot
from lib.llm.json_parser import parse_llm_json, save_dlq
from lib.llm_client import call_llm_with_retry, reranker_nli_verdict, reranker_score
from lib.pod_manager import ensure_sequential_dual
from lib.text_cleaner import get_cleaner
from lib.token_budget import TokenBudget
from lib.watchdog.messenger import heartbeat

TIMEOUT_ENRICH = 900  # default, overridden by _calc_timeout per-turn
MAX_TOKENS_ENRICH = 512
TEMP_ENRICH = 0.1
TOP_P_ENRICH = 0.9
TOP_K_ENRICH = 0  # 0 = disabled (unnecessary for classification)
REPEAT_PENALTY_ENRICH = 1.0
SHORT_TURN_THRESHOLD = 50
BATCH_LIMIT = 50
PARALLEL = 2  # concurrent LLM calls via ThreadPoolExecutor

# Dynamic timeout constants (extractor model on :8082)
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.15  # ~3 tok/s prefill for Korean chars
TIMEOUT_PER_TOK = 1.0  # ~1 tok/s decode
QUEUE_MARGIN = 2.0  # account for slot queuing with parallel=2
TIMEOUT_MAX = 3600  # absolute ceiling

SCHEMA_VERSION = 2  # increment on backward-incompatible enrich_meta changes

# Technology gazetteer — known tech names for entity extraction filtering
_GAZETTEER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "technology_gazetteer.yaml",
)
_TECHNOLOGY_GAZETTEER: frozenset[str] = frozenset()


def _load_technology_gazetteer() -> frozenset[str]:
    """Load technology gazetteer from YAML. Empty set on failure."""
    global _TECHNOLOGY_GAZETTEER
    if _TECHNOLOGY_GAZETTEER:
        return _TECHNOLOGY_GAZETTEER
    try:
        import yaml as _yaml

        with open(_GAZETTEER_PATH) as f:
            data = _yaml.safe_load(f)
        raw = data.get("technologies", []) if isinstance(data, dict) else []
        _TECHNOLOGY_GAZETTEER = frozenset(t.lower().strip() for t in raw if t and t.strip())
        print(f"  [gazetteer] loaded {len(_TECHNOLOGY_GAZETTEER)} technology names", flush=True)
    except Exception as e:
        print(f"  [gazetteer] load failed: {e} — using empty set", flush=True)
        _TECHNOLOGY_GAZETTEER = frozenset()
    return _TECHNOLOGY_GAZETTEER


def _get_git_short_hash() -> str:
    """Return short git hash for provenance stamping. Falls back to 'unknown'."""
    try:
        return subprocess.check_output(
            [
                "git",
                "-C",
                os.path.dirname(os.path.abspath(__file__)),
                "rev-parse",
                "--short",
                "HEAD",
            ],
            stderr=subprocess.DEVNULL,
            timeout=5,
            text=True,
        ).strip()
    except Exception:
        return "unknown"


SYSTEM_DAY_ENRICH = """\
You are a conversation analyst preparing structured metadata for an MCP
(Model Context Protocol) system. Given the original conversation turn and
the extracted facts, produce structured MCP fields.

SEQUENTIAL REASONING — Follow these steps internally:
Step 1 — SCAN: Read the turn and identify the core topic, intent, user sentiment, and any explicit entities (files, technologies, functions, users).
Step 2 — VERIFY: For each entity candidate, confirm it has a verbatim or near-verbatim match in the turn text. Discard hallucinated entities.
Step 3 — TLDR: Draft a one-line summary in the source language. Make it specific — capture what happened, not just that something happened.
Step 4 — FILTER: Remove vague tags and generic categories. Keep only tags that are specific, discoverable, and directly derivable.
Step 5 — CONFIDENCE: For each output field, estimate your confidence (0-100 integer). 90+ means clear unambiguous textual support; 70-89 means good support with some ambiguity; below 70 means limited evidence in the source.
Step 6 — OUTPUT: Produce the JSON below. Every field must be justified by the source.

CRITICAL — Entity Extraction Rules:
- files & functions: Require EXACT verbatim match in user_turn or text
- technologies: Require EXACT verbatim match of a well-known technology name (Python, PostgreSQL, llama.cpp, Kubernetes, etc.). Internal functions, variable names, and project-internal codenames are NOT technologies. If unsure, put in functions or omit.
- mentioned_users: Include only if a specific user/username is explicitly referenced
- Never hallucinate: if the conversation just "seems related" but doesn't clearly involve the entity, use empty array []

Examples for technologies (REJECT unless exact well-known technology verbatim match):
  - "taskset pinning 제거" → [] REJECT (taskset is a Linux command, not a technology)
  - "Postgres container" → ["PostgreSQL"] OK (well-known DB, alias recognized)
  - "llama-server --port 8082" → ["llama.cpp"] OK (well-known LLM runtime)
  - "ensure_model retry" → [] REJECT (internal function name, not a technology)
  - "와치독 개선" → [] REJECT (internal concept, not a technology)

Examples for files (REJECT unless exact match):
  - "night.py 파일 수정" → ["night.py"] OK (exact match)
  - "runner.py→" → ["runner.py"] OK (exact match)
  - "the pipeline script" → [] REJECT (vague reference)

Output STRICT JSON:
{
  "tldr": "source language로 한 줄 요약 (max 15 words)",
  "intent": "question|request|report|clarification|code_change|debug|design|other",
  "category": "requirement|decision|explanation|code|reasoning|other",
  "sentiment": "positive|negative|mixed|neutral",
  "sentiment_intensity": "low|medium|high",
  "entities": {
    "files": [],
    "technologies": [],
    "functions": [],
    "mentioned_users": []
  },
  "tags": [],
  "confidence": {
    "tldr": 0,
    "intent": 0,
    "category": 0,
    "entities": 0,
    "tags": 0
  }
}

RULES (strict — follow exactly):
- tldr: Use the SAME language as the turn's detected_lang (shown in the CONTEXT block below). If the turn contains both Korean and English, the tldr MUST be in Korean (the primary user language).
- tldr must be factual and directly derivable from the turn content
- Short turns (<50 chars) bypass LLM enrichment — intent classified via single-token call, tldr is the source text
- intent must be one of the enumerated values
- category: classify the turn's primary nature — requirement (new ask), decision (choice made), explanation (how/why), code (implementation), reasoning (analysis), other
- sentiment: classify user's emotional tone — positive (satisfied, appreciative), negative (frustrated, critical), mixed (both positive and negative elements), neutral (factual, no clear emotion)
- sentiment_intensity: low (subtle/mild), medium (clearly expressed), high (strong emotion, explicit language)
- entities: VERBATIM MATCH REQUIRED in user_turn or text
- entities.technologies: only technologies EXPLICITLY named
- entities.functions: function/class/method names EXPLICITLY mentioned
- tags: 2-5 keywords for discovery and routing
- confidence: rate each field 0-100 based on source evidence strength. Be calibrated — high confidence only when the source clearly supports the value
- If a field has no relevant data, use an empty array []"""

SYSTEM_ENRICH_VERIFY = """\
You are an MCP metadata verifier. Your job is to check the generated MCP fields
against the original conversation turn and fix errors.

Given:
  === INPUT: turn START ===
  user_turn / thinking / text
  === INPUT: turn END ===

  === CONTEXT: mcp START ===
  tldr / intent / entities / tags
  === CONTEXT: mcp END ===

Check each field:
  1. tldr: Accurate? Max 15 words? No markdown? If wrong, fix.
  2. intent: Matches the turn? Must be one of:
     question|request|report|clarification|code_change|debug|design|other
  3. category: Matches the turn's primary nature? Must be one of:
     requirement|decision|explanation|code|reasoning|other
  4. sentiment: Matches the user's tone? positive|negative|mixed|neutral
  5. entities.files: Only include files EXPLICITLY mentioned in the turn.
  6. entities.technologies: Technologies actually discussed.
  7. entities.functions: Function names actually mentioned.
  8. tags: Relevant to the turn? Max 5 tags.

Output corrected MCP JSON — same schema, only fix what's wrong.
Include a verdict block showing what changed.

Output:
{
  "tldr": "corrected summary",
  "intent": "corrected intent",
  "category": "corrected category",
  "sentiment": "positive|negative|mixed|neutral",
  "entities": {"files":[], "technologies":[], "functions":[], "mentioned_users":[]},
  "tags": ["tag1", "tag2"],
  "verdict": {"changes_made": false, "tldr_changed": false,
              "intent_changed": false, "category_changed": false,
              "sentiment_changed": false,
              "entities_changed": false, "tags_changed": false}
}"""

_VALID_INTENTS = {
    "question",
    "request",
    "report",
    "clarification",
    "code_change",
    "debug",
    "design",
    "other",
}
_VALID_CATEGORIES = {"requirement", "decision", "explanation", "code", "reasoning", "other"}

_SHORT_INTENT_PROMPT = """Classify the intent of this conversation turn.

Output EXACTLY one word from: {intents}

Turn: {source}"""


def _classify_short_intent(source: str) -> str:
    """Classify intent of a very short turn via single-token LLM call (max_tokens=1)."""
    if not source:
        return "other"
    prompt = _SHORT_INTENT_PROMPT.format(
        intents="|".join(sorted(_VALID_INTENTS)),
        source=source[:500],
    )
    try:
        meta = call_llm_with_retry(
            [{"role": "user", "content": prompt}],
            model="day_enrich",
            max_tokens=1,
            temperature=0.0,
            timeout=30,
            return_meta=True,
        )
        tok = meta["content"].strip().lower().rstrip(".,!?;:'\"")
        if tok in _VALID_INTENTS:
            return tok
    except Exception:
        pass
    return "other"


def _parse_json(raw: str, label: str = "enrich", attempt: int = 1,
                turn_id: str = "") -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM output using shared parse_llm_json + DLQ."""
    cleaned = strip_think(raw)
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(
            raw, stage=f"enrich_{label}", error="parse_llm_json returned None",
            attempt=attempt, turn_id=turn_id,
        )
    return result


def _clean_markdown(text: str) -> str:
    """Strip all markdown formatting from text."""
    if not text:
        return text
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"``.*?``", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*{2,}([^*]+)\*{2,}", r"\1", text)
    text = re.sub(r"_{2,}([^_]+)_{2,}", r"\1", text)
    text = re.sub(r"~{2,}([^~]+)~{2,}", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_markdown(text: str) -> str:
    """Remove markdown formatting from text."""
    if not text:
        return ""
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*{2,}([^*]+)\*{2,}", r"\1", text)
    text = re.sub(r"_{2,}([^_]+)_{2,}", r"\1", text)
    text = re.sub(r"~{2,}([^~]+)~{2,}", r"\1", text)
    text = text.replace("`", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _post_process_enrich(
    enrich_data: Optional[Dict[str, Any]], user_turn: str = "", text: str = ""
) -> Optional[Dict[str, Any]]:
    """Python post-processing for enrichment fields: validate, clean, trim, structural filter."""
    if not enrich_data:
        return enrich_data

    # tldr
    tldr = enrich_data.get("tldr", "")
    if tldr:
        tldr = _clean_markdown(tldr)
        tldr_h, _ = get_cleaner().hanja_substitute(tldr)
        tldr = tldr_h or tldr
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

    # entities — with structural pre-filter + fuzzy dedup
    entities = enrich_data.get("entities", {})
    if not isinstance(entities, dict):
        entities = {}
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = entities.get(key, [])
        if not isinstance(items, list):
            items = []
        seen: set = set()
        deduped: list = []
        for item in items:
            s = str(item).strip()
            if not s:
                continue
            # fuzzy dedup via SequenceMatcher
            if any(
                difflib.SequenceMatcher(None, s, existing).ratio() >= 0.85 for existing in deduped
            ):
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
            s_h, _ = get_cleaner().hanja_substitute(s)
            deduped.append(s_h or s)
        entities[key] = deduped[:10]
    # Cross-category dedup: priority functions > technologies > files
    # If a symbol is in functions, remove from technologies
    functions_set = set(e.lower() for e in entities.get("functions", []))
    entities["technologies"] = [
        t for t in entities.get("technologies", []) if t.lower() not in functions_set
    ]
    # mentioned_users overrides all other categories
    users_set = set(e.lower() for e in entities.get("mentioned_users", []))
    for cat in ("files", "technologies", "functions"):
        entities[cat] = [e for e in entities.get(cat, []) if e.lower() not in users_set]
    # Gazetteer filter: only known technologies survive
    gazetteer = _load_technology_gazetteer()
    if gazetteer:
        entities["technologies"] = [
            t for t in entities.get("technologies", []) if t.lower() in gazetteer
        ]
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

    # Schema version + provenance stamp
    enrich_data["schema_version"] = SCHEMA_VERSION
    if "provenance" not in enrich_data:
        enrich_data["provenance"] = _get_git_short_hash()

    return enrich_data


_ENTITY_REJECT_PATTERNS = [
    re.compile(r"https?://\S+"),
    re.compile(r"ftp://\S+"),
    re.compile(r"ftp\b"),
    re.compile(r"[\[\](){}]"),
    re.compile(r"^[\d\s]+$"),
]
_MAX_ENTITY_WORDS = 8
_MIN_ENTITY_LEN = 2
_ENTITY_SPECIAL_CHARS = re.compile(r"[@#$%^&*+=<>|\\~`;]")

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


# ── Entity grounding check (verbatim in source) ─────────────────────────


def _normalize(text: str) -> str:
    """Collapse whitespace, lowercase."""
    return " ".join(text.split()).lower()


def _check_entity_in_source(entity: str, source: str) -> bool:
    """Return True if entity appears verbatim (case-insensitive) in source."""
    if not entity or not source:
        return False
    return _normalize(entity) in _normalize(source)


def _verify_entities(entities: dict, user_turn: str, text: str) -> dict:
    """Check each entity: substring 1st → reranker for failures. Drop UNGROUNDED.

    Matches extract.py's NLI→reranker pattern: fast deterministic check first,
    then reranker (topical relevance) for uncertain cases via inference :8080.
    Falls back to substring-only if reranker unavailable.
    """
    source = f"{user_turn} {text}"
    rejected = {}
    verified = {}
    for key in ("files", "technologies", "functions", "mentioned_users"):
        items = entities.get(key, [])
        kept = []
        bad = []
        for item in items:
            if _check_entity_in_source(item, source):
                kept.append(item)
            else:
                try:
                    cos = reranker_score(item, source)
                    grounding = reranker_nli_verdict(cos)
                    if grounding == "UNGROUNDED":
                        bad.append(item)
                    else:
                        kept.append(item)
                except Exception:
                    # Reranker unavailable → accept entity (substring-only fallback)
                    kept.append(item)
        verified[key] = kept
        rejected[key] = bad
    return {
        "entities": verified,
        "rejected": rejected,
        "all_grounded": all(len(v) == 0 for v in rejected.values()),
    }


# ── TLDR NLI Self-Verify (LLM-based, same pattern as extract.py) ──
_NLI_TLDR_PROMPT = """You are verifying whether a TLDR summary accurately reflects the SOURCE conversation turn.

LABELS:
- ENTAILMENT: The TLDR is factually supported by the source (may be rephrased).
- CONTRADICTION: The TLDR contradicts the source — they cannot both be true.
- COMPLEMENTARY: The TLDR accurately summarizes or synthesizes source content without directly quoting it. This is normal for well-written summaries.
- NEUTRAL: The TLDR is related but not directly verifiable from the source.

Output EXACTLY one word: ENTAILMENT | CONTRADICTION | COMPLEMENTARY | NEUTRAL
No punctuation. No explanation.

SOURCE: {source}
TLDR: {tldr}"""


def _verify_tldr(tldr: str, user_turn: str, text: str) -> dict:
    """Run LLM NLI on tldr against source text. Returns verdict dict.

    Replaces the DeBERTa-v3 NLI server (port 8085 was never deployed).
    Uses the loaded extractor model on :8082 instead.
    """
    if not tldr or not (user_turn or text):
        return {"verdict": "SKIP", "label": "NEUTRAL", "score": 0.0}
    source = f"{user_turn}\n{text}"
    prompt = _NLI_TLDR_PROMPT.format(source=context_limit(source), tldr=tldr[:500])
    try:
        meta = call_llm_with_retry(
            [{"role": "user", "content": prompt}],
            model="day_enrich",
            max_tokens=16,
            temperature=0.0,
            timeout=60,
            return_meta=True,
        )
        raw = meta["content"].strip().upper()
        for tok in raw.replace("\n", " ").split():
            tok = tok.strip(".,!?;:\"'()[]")
            if tok in ("ENTAILMENT", "CONTRADICTION", "COMPLEMENTARY", "NEUTRAL"):
                return {"verdict": tok, "label": tok, "score": 0.0}
    except Exception:
        pass
    return {"verdict": "NEUTRAL", "label": "NEUTRAL", "score": 0.0}


def _fix_contradiction_tldr(
    user_turn: str, text: str, current_tldr: str, model: str
) -> Optional[str]:
    """Regenerate tldr when NLI detects CONTRADICTION. Max 2 targeted retries."""
    source = user_turn or text or ""
    if not source:
        return None
    prompt = (
        f"The TLDR below contradicts the SOURCE. Generate a corrected one-line summary "
        f"(max 15 words) that accurately reflects the SOURCE.\n\n"
        f"SOURCE: {context_limit(source, 2000)}\n"
        f"TLDR (CONTRADICTION): {current_tldr}\n\n"
        f"Corrected TLDR:"
    )
    for attempt in range(2):
        try:
            meta = call_llm_with_retry(
                [{"role": "user", "content": prompt}],
                model=model,
                max_tokens=64,
                temperature=0.3,
                timeout=60,
                return_meta=True,
            )
            tldr = meta["content"].strip()[:200]
            if tldr:
                return tldr
        except Exception:
            continue
    return None


def _calc_timeout(total_chars: int) -> int:
    """Dynamic timeout for enrich LLM call based on input size.

    Extractor model on 4-core ARM: prefill ~3 tok/s, decode ~1 tok/s.
    Korean chars ≈ 1 tok/char, so TIMEOUT_PER_CHAR=0.15 is ~6.7 chars/s.
    QUEUE_MARGIN accounts for parallel=2 slot queuing.
    """
    prompt_s = int(total_chars * TIMEOUT_PER_CHAR)
    decode_s = int(MAX_TOKENS_ENRICH * TIMEOUT_PER_TOK)
    est = TIMEOUT_BASE + prompt_s + decode_s
    est = int(est * QUEUE_MARGIN)
    return min(est, TIMEOUT_MAX)


def _generate_enrich_fields(
    user_turn: str,
    thinking: str,
    text: str,
    model: str = "day_enrich",
    extractions: Optional[List[Dict]] = None,
    detected_lang: str = "",
    timeout: Optional[int] = None,
    turn_id: str = "",
) -> Optional[Dict[str, Any]]:
    """Generate enrichment metadata fields (tldr, intent, entities, tags) via *model*.

    Uses TokenBudget priority allocation — sections dropped entirely if
    budget exceeded (no partial truncation).

    Returns dict with enrich fields + usage/timings metadata.
    """
    budget = TokenBudget("enrich")
    parts = []
    # TODO: CONTEXT가 text/facts 두 종류뿐이지만, 향후 enrich/review/mcp 등
    # 더 추가될 경우 CONTEXT_REF / CONTEXT_DERIVED / CONTEXT_RESULT 3분할 고려.
    user_turn_content = user_turn or text or "(empty)"
    if budget.add_section("user_turn", user_turn_content, priority=10):
        parts.append("=== INPUT: user_turn START ===")
        parts.append(user_turn_content)
        parts.append("=== INPUT: user_turn END ===")

    if budget.add_section("thinking", thinking or "(empty)", priority=4):
        parts.append("")
        parts.append("<<< REASONING: thinking START >>>")
        parts.append(thinking or "(empty)")
        parts.append("<<< REASONING: thinking END >>>")

    if budget.add_section("text", text or "(empty)", priority=7):
        parts.append("")
        parts.append("=== CONTEXT: text START ===")
        parts.append(text or "(empty)")
        parts.append("=== CONTEXT: text END ===")

    if detected_lang:
        parts.append("")
        parts.append("=== CONTEXT: detected_lang START ===")
        parts.append(detected_lang)
        parts.append("=== CONTEXT: detected_lang END ===")

    if extractions:
        parts.append("")
        parts.append("=== CONTEXT: facts START ===")
        for idx, ex in enumerate(extractions):
            confidence = ex.get("fact_confidence", 100)
            line = (
                f"  [{ex.get('fact_type', '?')}] (conf={confidence}) {ex.get('evidence', '')[:300]}"
            )
            if budget.add_section(f"extract_{idx}", line, priority=6):
                parts.append(line)
        parts.append("=== CONTEXT: facts END ===")
    parts.append("")
    if budget.used > 0:
        parts.append(f"[context budget: {budget.used}/{budget.limit} tok]")

    # Static few-shot from YAML (diversity-first, KV-cache-optimized)
    system_content = SYSTEM_DAY_ENRICH
    feedback_text = load_few_shot()
    if feedback_text:
        system_content = SYSTEM_DAY_ENRICH + "\n\n" + feedback_text

    t = timeout if timeout is not None else TIMEOUT_ENRICH
    meta = call_llm_with_retry(
        [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "\n".join(parts)},
        ],
        model=model,
        max_tokens=MAX_TOKENS_ENRICH,
        temperature=TEMP_ENRICH,
        top_p=TOP_P_ENRICH,
        top_k=TOP_K_ENRICH,
        repeat_penalty=REPEAT_PENALTY_ENRICH,
        timeout=t,
        json_mode=True,
        chat_template_kwargs={"enable_thinking": False},
        return_meta=True,
    )
    result = _parse_json(meta["content"], "enrich fields", turn_id=turn_id)
    if result:
        result["_meta"] = {
            "usage": meta["usage"],
            "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"],
            "model": model,
        }
    return result


def _get_turns_without_enrich(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Atomically claim extracted turns via FOR UPDATE SKIP LOCKED,
    set pipeline_state='enriching', and return turn data.
    Also skips turns with unresolved NEUTRAL facts (no user_verdict yet).
    """
    sql = f"""
        WITH claimed AS (
            UPDATE turns SET pipeline_state = 'enriching'
            WHERE id IN (
                SELECT t.id
                FROM turns t
                WHERE NOT EXISTS (
                    SELECT 1 FROM review_facts rf2
                    WHERE rf2.turn_id = t.id AND rf2.fact_type = 'enrich_meta'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM review_facts rf3
                    WHERE rf3.turn_id = t.id
                    AND rf3.nli_llm = 'NEUTRAL'
                    AND rf3.user_verdict IS NULL
                    AND rf3.nli_verdict = 'AMBIGUOUS'
                )
                AND t.pipeline_state = 'verified'
                ORDER BY t.est_chars ASC NULLS LAST, t.created_at DESC
                LIMIT {limit}
                FOR UPDATE SKIP LOCKED
            )
            RETURNING turns.id, turns.user_turn, turns.thinking, turns.text,
                      turns.detected_lang, turns.created_at, turns.est_chars
        )
        SELECT row_to_json(r.*) FROM (
            SELECT id, user_turn, thinking, text, detected_lang, created_at, est_chars
            FROM claimed
        ) r
        ORDER BY r.est_chars ASC NULLS LAST, r.created_at DESC
    """
    raw = psql(sql)
    if not raw:
        return []
    import json as _json

    rows = []
    for line in raw.strip().split("\n"):
        line = line.strip()
        if line:
            try:
                rows.append(_json.loads(line))
            except _json.JSONDecodeError:
                continue
    if not rows:
        return []
    return [
        {
            "id": r.get("id", ""),
            "user_turn": r.get("user_turn", ""),
            "thinking": r.get("thinking") or None,
            "text": r.get("text", ""),
            "detected_lang": r.get("detected_lang"),
            "created_at": r.get("created_at", ""),
            "est_chars": r.get("est_chars", 0),
        }
        for r in rows
    ]


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
    return [
        {
            "fact_type": r.get("fact_type", "text"),
            "evidence": r.get("evidence", ""),
            "fact_confidence": r.get("fact_confidence", 100),
        }
        for r in rows
    ]


def _insert_enrich_fact(
    turn_id: str,
    fact_index: int,
    enrich_json_str: str,
    model: str,
    prompt_tokens: Optional[int] = None,
    gen_tokens: Optional[int] = None,
    elapsed_ms: Optional[float] = None,
    source_file: Optional[str] = None,
) -> bool:
    """Insert an enrich_meta fact row into review_facts."""
    cols = [
        "turn_id",
        "fact_index",
        "fact_type",
        "evidence",
        "extract_model",
        "verdict",
        "source",
        "fact_action",
        "fact_confidence",
    ]
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


def enrich_pipeline(
    turn_id: Optional[str] = None,
    limit: int = BATCH_LIMIT,
    dry_run: bool = False,
    model: str = "day_enrich",
) -> Dict[str, Any]:
    """Generate enrichment metadata for turns with extraction facts."""
    t_start = time.monotonic()
    heartbeat("day_enrich", "pipeline_start")
    print(f"\n{'=' * 60}")
    print(f"Enrich Pipeline — {model} enrich fields for extracted turns")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    # Select turns
    if turn_id:
        sql = (
            "SELECT t.id, t.user_turn, t.thinking, t.text, t.created_at, t.est_chars "
            f"FROM turns t WHERE t.id = '{esc_sql(turn_id)}'::uuid"
        )
        rows = psql_json(sql)
        if not rows:
            print(f"[enrich] Turn not found: {turn_id}")
            return {"processed": 0, "failed": 1, "ok": False}
        r = rows[0]
        turns = [
            {
                "id": r["id"],
                "user_turn": r.get("user_turn", ""),
                "thinking": r.get("thinking") or None,
                "text": r.get("text", ""),
                "created_at": r.get("created_at", ""),
                "est_chars": r.get("est_chars", 0),
            }
        ]
    else:
        turns = _get_turns_without_enrich(limit)

    if not turns:
        print("[enrich] No turns without enrichment found")
        return {"processed": 0, "failed": 0, "ok": True}

    print(f"[enrich] Processing {len(turns)} turn(s)", flush=True)

    processed = 0
    failed = 0
    n = len(turns)

    # ── Phase 0: Pre-load extractions for all turns (fast, no LLM) ──
    turn_data = []
    for ti, turn in enumerate(turns, 1):
        turn_id = turn["id"]
        user_turn = turn.get("user_turn", "") or ""
        thinking = turn.get("thinking", "") or ""
        text = turn.get("text", "") or ""
        est_chars = turn.get("est_chars", 0) or 0
        detected_lang = turn.get("detected_lang", "") or ""
        extractions = _get_turn_extractions(turn_id)
        turn_data.append(
            (turn_id, user_turn, thinking, text, extractions, est_chars, detected_lang)
        )

    if not turn_data:
        print("[enrich] No turns with extraction context found")
        return {"processed": 0, "failed": 0, "elapsed_s": 0, "ok": True}

    # ── Helper: process one completed LLM result (post-process → verify → store) ──
    def _store_result(ti, tid, ut, tx, enrich_result, dr):
        try:
            if not enrich_result:
                print(f"  [{ti}/{n}] {tid[:8]} — LLM returned None, skipping", flush=True)
                return False

            # Phase 2: Post-processing
            enrich_result = _post_process_enrich(enrich_result, ut, tx)

            # Phase 3: Entity grounding
            entities_data = enrich_result.get("entities", {}) or {}
            verified_entities = _verify_entities(entities_data, ut, tx)
            enrich_result["entities"] = verified_entities["entities"]
            if any(verified_entities["rejected"].values()):
                print(
                    f"  [{ti}/{n}] {tid[:8]} — rejected entities: "
                    + "; ".join(
                        f"{k}: {v}"
                        for k, vals in verified_entities["rejected"].items()
                        if (v := vals)
                    ),
                    flush=True,
                )

            # Phase 3b: TLDR NLI
            enrich_tldr = enrich_result.get("tldr", "") or ""
            tldr_verify = _verify_tldr(enrich_tldr, ut, tx)
            enrich_result["nli_verdict"] = tldr_verify["verdict"]
            if tldr_verify["verdict"] == "CONTRADICTION":
                print(f"  [{ti}/{n}] {tid[:8]} — ⚠ tldr CONTRADICTION, fixing...", flush=True)
                fixed = _fix_contradiction_tldr(ut, tx, enrich_tldr, model)
                if fixed:
                    # Clean and re-verify
                    fixed = _clean_markdown(fixed)
                    fixed_h, _ = get_cleaner().hanja_substitute(fixed)
                    enrich_result["tldr"] = (fixed_h or fixed)[:200]
                    tldr_verify = _verify_tldr(enrich_result["tldr"], ut, tx)
                    enrich_result["nli_verdict"] = tldr_verify["verdict"]
                    if tldr_verify["verdict"] == "CONTRADICTION":
                        enrich_result["tldr"] = (ut or tx or "").strip()[:200]
                        enrich_result["nli_verdict"] = "FALLBACK"
                        print(
                            f"  [{ti}/{n}] {tid[:8]} — CONTRADICTION persists, source fallback",
                            flush=True,
                        )
                    else:
                        print(
                            f"  [{ti}/{n}] {tid[:8]} — tldr fixed ({tldr_verify['verdict']})",
                            flush=True,
                        )
                else:
                    enrich_result["tldr"] = (ut or tx or "").strip()[:200]
                    enrich_result["nli_verdict"] = "FALLBACK"
                    print(
                        f"  [{ti}/{n}] {tid[:8]} — CONTRADICTION retry failed, source fallback",
                        flush=True,
                    )

            # Log
            print(
                f"  [{ti}/{n}] {tid[:8]} — tldr={enrich_result.get('tldr', '')[:60]} "
                f"intent={enrich_result.get('intent', '?')} "
                f"entities={len(enrich_result.get('entities', {}).get('files', []))}f/"
                f"{len(enrich_result.get('entities', {}).get('functions', []))}fn",
                flush=True,
            )

            if dr:
                print("    [DRY] Would store", flush=True)
                return True

            # Store to DB
            fi_sql = (
                f"SELECT COALESCE(MAX(fact_index), -1) + 1 FROM review_facts "
                f"WHERE turn_id = '{esc_sql(tid)}'::uuid"
            )
            fi_str = psql(fi_sql)
            fi = int(fi_str) if fi_str and fi_str != "-infinity" else 0

            meta = enrich_result.get("_meta", {})
            usage = meta.get("usage", {}) if meta else {}
            _insert_enrich_fact(
                tid,
                fi,
                json.dumps(
                    {k: v for k, v in enrich_result.items() if k != "_meta"}, ensure_ascii=False
                ),
                model,
                prompt_tokens=usage.get("prompt_tokens"),
                gen_tokens=usage.get("completion_tokens"),
                elapsed_ms=meta.get("elapsed_ms") if meta else None,
            )
            psql_ok(f"UPDATE turns SET pipeline_state = 'enriched' WHERE id = '{tid}'::uuid")
            return True
        except Exception as e:
            print(f"  [{ti}/{n}] {tid[:8]} — ERROR: {type(e).__name__}: {e}", flush=True)
            return False

    # ── Phase 1: LLM generation (pool first, then solo large turns) ──
    MAX_CHARS_SOLO = 5000
    pool_items = []  # (ti, tid, ut, th, tx, exts, est_c)
    solo_items = []  # same structure
    print(
        f"[enrich] Processing {len(turn_data)} turns (solo threshold={MAX_CHARS_SOLO} chars)...",
        flush=True,
    )
    llm_t0 = time.monotonic()

    # Separate short vs normal vs solo — short turns bypass LLM entirely
    short_items = []  # (ti, tid, ut, tx)
    for ti, (tid, ut, th, tx, exts, est_c, dl) in enumerate(turn_data, 1):
        if (est_c or 0) > 0 and (est_c or 0) < SHORT_TURN_THRESHOLD:
            short_items.append((ti, tid, ut, tx))
        elif est_c > MAX_CHARS_SOLO:
            solo_items.append((ti, tid, ut, th, tx, exts, est_c, dl))
        else:
            pool_items.append((ti, tid, ut, th, tx, exts, est_c, dl))

    # Short turns — single-token intent classification, source as tldr
    for ti, tid, ut, tx in short_items:
        tldr = (ut or tx or "").strip()[:200]
        source = ut or tx or ""
        short_intent = _classify_short_intent(source)
        enrich_result = {"tldr": tldr, "intent": short_intent, "entities": {}, "tags": []}
        ok = _store_result(ti, tid, ut, tx, enrich_result, dry_run)
        if ok:
            processed += 1
            print(
                f"  [{ti}/{n}] {tid[:8]} — intent={short_intent} (short turn, single-token)",
                flush=True,
            )
        else:
            failed += 1

    # Normal turns via ThreadPool — dual model A/B round-robin
    if pool_items:
        _models = [model, f"{model}_b"]
        with ThreadPoolExecutor(max_workers=len(_models)) as pool:
            fut_map = {}
            for idx, (ti, tid, ut, th, tx, exts, est_c, dl) in enumerate(pool_items):
                total_chars = est_c
                call_timeout = _calc_timeout(total_chars)
                m = _models[idx % len(_models)]
                fut = pool.submit(
                    _generate_enrich_fields,
                    ut,
                    th,
                    tx,
                    model=m,
                    extractions=exts,
                    detected_lang=dl,
                    timeout=call_timeout,
                    turn_id=tid,
                )
                fut_map[fut] = (ti, tid, ut, tx, m)

            for fut in as_completed(fut_map):
                ti, tid, ut, tx, _m = fut_map[fut]
                try:
                    result = fut.result()
                    ok = _store_result(ti, tid, ut, tx, result, dry_run)
                    if ok:
                        processed += 1
                        heartbeat("day_enrich", f"turn {tid[:8]} done")
                    else:
                        failed += 1
                except Exception as e:
                    print(
                        f"  [{ti}/{n}] {tid[:8]} — LLM call failed: {type(e).__name__}: {e}",
                        flush=True,
                    )
                    failed += 1

    # Solo large turns after (slow path, doesn't delay pool)
    for ti, tid, ut, th, tx, exts, est_c, dl in solo_items:
        total_chars = est_c
        call_timeout = _calc_timeout(total_chars)
        print(f"  [{ti}/{n}] {tid[:8]} — large turn ({est_c} chars), solo after pool", flush=True)
        try:
            result = _generate_enrich_fields(
                ut,
                th,
                tx,
                model=model,
                extractions=exts,
                detected_lang=dl,
                timeout=call_timeout,
                turn_id=tid,
            )
            ok = _store_result(ti, tid, ut, tx, result, dry_run)
            if ok:
                processed += 1
                heartbeat("day_enrich", f"turn {tid[:8]} done")
            else:
                failed += 1
        except Exception as e:
            print(f"  [{ti}/{n}] {tid[:8]} — solo LLM failed: {type(e).__name__}: {e}", flush=True)
            failed += 1

    llm_elapsed = round(time.monotonic() - llm_t0, 1)
    elapsed = round(time.monotonic() - t_start, 1)
    print(f"\n{'=' * 60}", flush=True)
    print(
        f"Done: {processed} enriched, {failed} failed (LLM: {llm_elapsed}s, total: {elapsed}s)",
        flush=True,
    )
    if dry_run:
        print("  [DRY RUN] No data was written", flush=True)
    print(f"{'=' * 60}", flush=True)

    return {"processed": processed, "failed": failed, "elapsed_s": elapsed, "ok": failed == 0}


def main() -> None:
    from lib.infra.preflight import preflight_checks

    preflight_checks("enrich.py")
    ensure_sequential_dual("day-enricher", "day-enricher-b")
    import argparse

    parser = argparse.ArgumentParser(
        description="Enrich Pipeline — generate enrichment fields for extracted turns"
    )
    parser.add_argument("--turn-id", help="Process a specific turn UUID")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--model",
        default="day_enrich",
        help="Model for enrichment fields generation (default: day_enrich)",
    )
    args = parser.parse_args()

    result = enrich_pipeline(
        turn_id=args.turn_id,
        limit=args.limit,
        dry_run=args.dry_run,
        model=args.model,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    from lib.llm_client import recall_tiny

    recall_tiny()
    # Kill all llama-server instances to free memory before next pipeline stage
    from pipelines.extract_llm import _cleanup_all_llms

    _cleanup_all_llms()
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
