#!/usr/bin/env python3
# Status: production
# Path: extract.py — submodule for LLM extraction
"""LLM extraction submodule for the Extract Pipeline.

Contains all LLM interaction code: system prompts, 8082 recovery,
_extract_section, _extract_single, _extract_for_turn,
_extract_solo_section_major (section-major KV cache batch),
JSON parsing, entity context loading, low-value filter.
"""

import json
import math
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import strip_think, context_limit
from lib.db import esc_sql, psql_json
from lib.llm.json_parser import save_dlq, parse_llm_json
from lib.llm_client import call_llm, MODEL_REGISTRY
from lib.pod_manager import ensure_model as _ensure_model_pod
from lib.watchdog.messenger import heartbeat

_8082_RECOVERY_LOCK = threading.Lock()

# ── 8082 Auto-Recovery ──────────────────────────────────────────

_CONNECTION_ERROR_SUBSTRINGS = (
    "Remote end closed", "Connection reset", "Connection refused",
    "Broken pipe", "RemoteDisconnected",
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
        _ensure_model_pod('day-extractor', skip_if_healthy=False)
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

# ── Constants ────────────────────────────────────────────────────

TIMEOUT_EXTRACT = 900
MAX_TOKENS_BASE = 256     # minimum (MIN_USEFUL_TOKENS=256과 동기화)
TOKENS_PER_300CH = 50     # 300ch당 약 1 fact 추가, ceil 적용
TEMP_EXTRACT = 0.0
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.2
TIMEOUT_PER_TOK = 1.2     # ~0.83 tok/s decode (20% safety margin)
GEN_TIME_BUF = 90          # spike/GC/swap buffer
CAP = 1800                 # hard cap (절대 초과 금지)
MIN_USEFUL_TOKENS = 256    # 이 미만이면 명시적 거절
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
            except (IOError, ValueError):
                break
        print(f"\n  [SIGTERM] from parent chain: {' > '.join(chain)}", flush=True)
    except Exception:
        print(f"\n  [SIGTERM] (source chain unavailable)", flush=True)
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

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "The cook preheated the oven to 180 degrees Celsius.",
      "category": "other",
      "subject": "cook",
      "predicate": "preheats_oven_to",
      "object": "180 degrees Celsius",
      "qualifiers": {"unit": "celsius"},
      "source_context": "The recipe says to bake at 180 degrees."
    },
    {
      "evidence": "The server sets the database connection pool to 10 connections.",
      "category": "code",
      "subject": "server",
      "predicate": "configures_pool_size_to",
      "object": "10",
      "qualifiers": {"unit": "connections"},
      "source_context": "The database config sets pool_size to 10."
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

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "The researcher mixed the solution at 50 degrees Celsius.",
      "category": "other",
      "subject": "researcher",
      "predicate": "mixes_solution_at",
      "object": "50 degrees Celsius",
      "qualifiers": {"unit": "celsius"},
      "source_context": "The lab protocol states the mixing temperature as 50 degrees."
    },
    {
      "evidence": "The pipeline checks the database for pending turns before starting extraction.",
      "category": "code",
      "subject": "pipeline",
      "predicate": "checks_database_for",
      "object": "pending turns",
      "qualifiers": {"status": "pending"},
      "source_context": "Before running extraction, the pipeline queries turns with pipeline_state=pending."
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
   "port 8082" → "the Pod B extractor runs on port 8082"

3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a
   snake_case verb phrase describing the relation (see STRUCTURED FIELDS above).

4. SIGNIFICANCE: Extract only specific, non-obvious, informative facts.
   Do NOT extract trivial statements about conversation flow.

5. FAITHFULNESS: Directly traceable to source text. NO inference or hallucination.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "The chef seasoned the steak with salt and pepper.",
      "category": "other",
      "subject": "chef",
      "predicate": "seasons_with",
      "object": "salt and pepper",
      "qualifiers": {},
      "source_context": "The recipe instructs the chef to season both sides."
    },
    {
      "evidence": "The application logs a warning when memory usage exceeds 80 percent.",
      "category": "code",
      "subject": "application",
      "predicate": "logs_warning_when",
      "object": "memory usage exceeds 80 percent",
      "qualifiers": {"threshold": "80%"},
      "source_context": "In the health check module, a warning is emitted at 80% memory usage."
    }
  ]
}

- Extract at least 1 fact if there is meaningful content.
- If nothing extractable, return {"extractions": []}.
- NOISE DETECTION: keyboard smash, gibberish, API error messages,
  meaningless text → return {"skip_verdict": "skip"}."""

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
        save_dlq(raw, stage=f"extract_{label}", error="parse_llm_json returned None",
                 attempt=attempt)
    return result


# Backward compat aliases for test files
SYSTEM_DAY_EXTRACT = _SYSTEM_TEXT_EXTRACT


def _extract_section(section_type: str, source_text: str,
                     pulse_context: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
        print(json.dumps({"event": "max_tokens_overflow", "text_len": text_len,
                          "overhead_sec": overhead, "cap_sec": CAP,
                          "reason": "prefill_exceeds_cap"}), flush=True)
        return None  # prefill만으로 CAP 초과
    achievable = int((CAP - overhead) / TIMEOUT_PER_TOK)
    if achievable < MIN_USEFUL_TOKENS:
        print(json.dumps({"event": "max_tokens_overflow", "text_len": text_len,
                          "achievable": achievable, "min_useful": MIN_USEFUL_TOKENS,
                          "reason": "below_min_useful"}), flush=True)
        return None  # 생성 가능 token이 너무 적음 → 명시적 거절

    final = min(wanted, achievable)
    if final < wanted:
        print(json.dumps({"event": "tokens_truncated", "wanted": wanted,
                          "achievable": achievable, "text_len": text_len}), flush=True)
    return final


def _calc_timeout(total_chars: int, max_tokens: int) -> int:
    est = (TIMEOUT_BASE + int(total_chars * TIMEOUT_PER_CHAR)
           + int(max_tokens * TIMEOUT_PER_TOK) + GEN_TIME_BUF)
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
    all_found = bool(files or functions or classes or libraries or models_list or variables or services)
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


def _extract_single(section_type: str, source_text: str,
                    pulse_context: Optional[str] = None,
                    timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
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
            print(json.dumps({"event": "extract_skip_overflow",
                              "text_len": len(source_text),
                              "max_input_advertised": MAX_INPUT_CHARS_ADVERTISED}), flush=True)
            return None
        timeout = _calc_timeout(len(source_text), max_tokens=max_tok)

    meta = _call_with_8082_retry(
        call_llm,
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": source_text}],
        model="day_extract",
        max_tokens=max_tok, temperature=TEMP_EXTRACT,
        timeout=timeout, json_mode=True, return_meta=True,
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
        return {"extractions": [], "skip": True, "usage": meta["usage"], "timings": meta["timings"],
                "elapsed_ms": meta["elapsed_ms"]}
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
        print(f"  [_extract_single] dropped {before_lv - len(ex)} low-value evidence(s)", flush=True)
    return {"extractions": ex, "usage": meta["usage"], "timings": meta["timings"],
            "elapsed_ms": meta["elapsed_ms"]}


# ── Per-turn extraction (user → thinking → text) ────────────────


def _merge_usage(target: Dict[str, int], usage: Dict) -> None:
    if not usage:
        return
    for k in ("prompt_tokens", "completion_tokens"):
        v = usage.get(k, 0) or 0
        target[k] = (target.get(k, 0) or 0) + v


def _extract_for_turn(turn: dict, pulse_context: Optional[str] = None) -> Tuple[dict, Optional[Dict[str, Any]], Optional[str]]:
    user_turn = turn.get("user_turn") or ""
    thinking = turn.get("thinking") or ""
    text = turn.get("text") or ""

    entity_context = _load_entity_context(turn.get("id", ""))

    def _robust_extract(section_type, source_text, pulse_context=None, max_attempts=2):
        for attempt in range(max_attempts):
            try:
                return _extract_section(section_type, source_text, pulse_context=pulse_context)
            except Exception as e:
                print(f"      [{section_type}] attempt {attempt+1}/{max_attempts} failed: {e}", flush=True)
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
            print(f"      [user] noise skip", flush=True)
        elif res and res.get("extractions"):
            all_extractions.extend(res["extractions"])
            _merge_usage(total_usage, res.get("usage", {}))
            total_elapsed_ms += time.monotonic() - t0
            print(f"      [user] {len(res['extractions'])} facts ({time.monotonic()-t0:.0f}s)", flush=True)
        elif res is None:
            print(f"      [user section] failed after retries", flush=True)
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
                f"{thinking_context}\n\n{entity_context}"
                if entity_context else thinking_context
            )
        res = _robust_extract("text", text, pulse_context=combined_context)
        if res and res.get("skip"):
            any_skip = True
            print(f"      [text] noise skip", flush=True)
        elif res and res.get("extractions"):
            all_extractions.extend(res["extractions"])
            _merge_usage(total_usage, res.get("usage", {}))
            total_elapsed_ms += time.monotonic() - t0
            print(f"      [text] {len(res['extractions'])} facts ({time.monotonic()-t0:.0f}s)", flush=True)
        elif res is None:
            print(f"      [text section] failed after retries", flush=True)

    if not all_extractions and any_skip:
        return (turn, None, "noise skip")
    if not all_extractions:
        return (turn, None, "all sections returned empty")

    return (turn, {"extractions": all_extractions, "usage": total_usage,
                   "timings": {}, "elapsed_ms": total_elapsed_ms}, None)


# ── Section-major solo processing ───────────────────────────────


def _checkpoint_sections(extractions):
    return set(e.get("fact_type") for e in extractions if e.get("fact_type") in ("user", "thinking", "text"))


def _extract_solo_section_major(
    solo_turns: List[dict],
    pulse_context: Optional[str] = None,
    dry_run: bool = False,
) -> Generator[Tuple[dict, Optional[Dict], Optional[str]], None, None]:
    # Lazy import to avoid circular dependency (extract.py imports us)
    from extract import _save_checkpoint, _load_checkpoint

    # Build entity context for text section (includes thinking as context per F-CoT)
    entity_map = {}
    for t in solo_turns:
        base_ctx = _load_entity_context(t["id"]) or ""
        thinking = (t.get("thinking") or "").strip()
        if thinking:
            tc = f"<<< REASONING: thinking START >>>\n{context_limit(thinking)}\n<<< REASONING: thinking END >>>"
            entity_map[t["id"]] = f"{tc}\n\n{base_ctx}" if base_ctx else tc
        else:
            entity_map[t["id"]] = base_ctx or None

    turn_data: Dict[str, dict] = {}
    for t in solo_turns:
        ckpt = _load_checkpoint(t["id"]) if not dry_run else None
        if ckpt:
            turn_data[t["id"]] = {"extractions": ckpt, "total_usage": {}, "skip": False}
        else:
            turn_data[t["id"]] = {"extractions": [], "total_usage": {}, "skip": False}

    solo_t0 = time.monotonic()

    def _merge_section(res, t):
        if res and res.get("skip"):
            turn_data[t["id"]]["skip"] = True
        elif res and res.get("extractions"):
            turn_data[t["id"]]["extractions"].extend(res["extractions"])
            _merge_usage(turn_data[t["id"]]["total_usage"], res.get("usage", {}))

    def _has_section(t, section_type):
        return any(e.get("fact_type") == section_type
                   for e in turn_data[t["id"]]["extractions"])

    def _batch_extract(section_type, source_getter) -> None:
        targets = [(t, source_getter(t))
                   for t in solo_turns
                   if source_getter(t) and not _has_section(t, section_type)]
        if not targets:
            return
        if dry_run:
            for t, _ in targets:
                print(f"  [{section_type}] {t['id'][:8]} (dry-run skip)", flush=True)
            return

        print(f"  [solo] {section_type} section: {len(targets)} turns", flush=True)

        for t, src in targets:
            try:
                ctx = entity_map.get(t["id"]) if section_type == "text" else None
                res = _extract_section(section_type, src, pulse_context=ctx)
                _merge_section(res, t)
            except Exception as e:
                print(f"  [{section_type}] {t['id'][:8]} failed: {e}", flush=True)

    # Phase 1: user section
    _batch_extract("user", lambda t: t.get("user_turn", "") or "")
    if not dry_run:
        for t in solo_turns:
            if turn_data[t["id"]]["extractions"]:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
    heartbeat("day_extract", "solo user done")
    time.sleep(6)

    # Phase 2: text section (thinking is NOT extracted — used as context above)
    _batch_extract("text", lambda t: t.get("text") or "")
    if not dry_run:
        for t in solo_turns:
            if turn_data[t["id"]]["extractions"]:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
    heartbeat("day_extract", f"solo text done, total={time.monotonic()-solo_t0:.0f}s")

    for t in solo_turns:
        td = turn_data[t["id"]]
        if td.get("skip") and not td["extractions"]:
            yield t, None, "noise skip"
        elif not td["extractions"]:
            yield t, None, "all sections returned empty"
        else:
            yield t, {"extractions": td["extractions"],
                       "usage": td["total_usage"],
                       "timings": {}, "elapsed_ms": 0}, None
