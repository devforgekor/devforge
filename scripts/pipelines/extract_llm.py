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
import os
import re
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.common import strip_think
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
MAX_TOKENS_EXTRACT = 512
TEMP_EXTRACT = 0.0
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.2
MAX_CHARS_SOLO = 5000
SOLO_TIMEOUT_FACTOR = 2.5
GEN_TIME_BUF = 450

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


# ── Section-specific System prompts ─────────────────────────────

_SYSTEM_USER_EXTRACT = """\
You are a fact extractor for a developer conversation. Given a USER MESSAGE,
extract key factual statements that are EXPLICITLY present in the message.

Do NOT infer, summarize, or add information not present in the source.

SEQUENTIAL REASONING — Follow these steps internally:
Step 1 — SCAN: Locate passages with specific, factual claims about code, config, decisions, requirements, or explanations.
Step 2 — VERIFY: For each candidate, confirm it is EXPLICITLY stated in the source. Discard any hallucinated or inferred content.
Step 3 — RESOLVE: Make each candidate self-contained. Replace pronouns ("it", "this", "that") and implicit references with the specific entities.
Step 4 — FILTER: Keep only specific, informative, non-obvious facts. Drop trivial statements about conversation flow or common knowledge.
Step 5 — OUTPUT: Produce the JSON below.

CRITICAL — Self-Contained Evidence Rule:
Each evidence sentence MUST be self-contained. Resolve pronouns ("it", "this", "that")
and implicit references. If the evidence refers to a specific concept, file, or
person mentioned in the surrounding context, include that referent explicitly.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "Self-contained factual statement (resolve pronouns)",
      "category": "requirement|decision|explanation|code|reasoning|other",
      "source_context": "Surrounding 1-2 sentences that provide context — helps disambiguate this evidence"
    }
  ]
}

Rules:
- evidence must be directly traceable to the source text
- evidence must be self-contained: "it uses port 8082" → "the LLM server uses port 8082"
- evidence MUST be a complete, grammatically valid sentence — do NOT output fragments or truncated text
- SIGNIFICANCE: Do NOT extract trivial/obvious statements. A fact is low-value if an observer could deduce it just from knowing the conversation exists. Extract only specific, non-obvious, informative facts.
- source_context: include the surrounding sentence(s) that clarify pronouns, references, or conditions
- Extract at least 1 fact if there is meaningful content
- If nothing extractable, return {"extractions": []}
- NOISE DETECTION: If the message is keyboard smash, gibberish, API error message (e.g. "API Error: ECONNRESET", "ConnectionRefused"), or otherwise meaningless text (e.g. "ㅑ다냐졷ㄷ", "asdfasdf"), return {"skip_verdict": "skip"} — do NOT extract facts from noise."""

_SYSTEM_THINKING_EXTRACT = """\
You are a fact extractor for a developer conversation. Given the ASSISTANT'S
INTERNAL REASONING (thinking), extract key factual statements.

Do NOT infer, summarize, or add information not present in the source.

SEQUENTIAL REASONING — Follow these steps internally:
Step 1 — SCAN: Locate passages with specific, factual claims about code, config, decisions, requirements, or explanations.
Step 2 — VERIFY: For each candidate, confirm it is EXPLICITLY stated in the source. Discard any hallucinated or inferred content.
Step 3 — RESOLVE: Make each candidate self-contained. Replace pronouns ("it", "this", "that") and implicit references with the specific entities.
Step 4 — FILTER: Keep only specific, informative, non-obvious facts. Drop trivial statements about conversation flow or common knowledge.
Step 5 — OUTPUT: Produce the JSON below.

CRITICAL — Self-Contained Evidence Rule:
Each evidence sentence MUST be self-contained. Resolve pronouns ("it", "this", "that")
and implicit references. If the evidence refers to a specific concept, file, or
person mentioned in the surrounding context, include that referent explicitly.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "Self-contained factual statement (resolve pronouns)",
      "category": "requirement|decision|explanation|code|reasoning|other",
      "source_context": "Surrounding 1-2 sentences that provide context — helps disambiguate this evidence"
    }
  ]
}

Rules:
- evidence must be directly traceable to the source text
- evidence must be self-contained: "add an index" → "the user requested adding a database index"
- evidence MUST be a complete, grammatically valid sentence — do NOT output fragments or truncated text
- SIGNIFICANCE: Do NOT extract trivial/obvious statements. Extract only specific, non-obvious, informative facts.
- source_context: include the surrounding sentence(s) that clarify pronouns, references, or conditions
- Extract at least 1 fact if there is meaningful content
- If thinking is empty or contains only formatting, return {"extractions": []}
- NOISE DETECTION: If the message is keyboard smash, gibberish, API error message (e.g. "API Error: ECONNRESET", "ConnectionRefused"), or otherwise meaningless text (e.g. "ㅑ다냐졷ㄷ", "asdfasdf"), return {"skip_verdict": "skip"} — do NOT extract facts from noise."""

_SYSTEM_TEXT_EXTRACT = """\
You are a fact extractor for a developer conversation. Given the ASSISTANT'S
RESPONSE (text), extract key factual statements that are EXPLICITLY present.

Do NOT infer, summarize, or add information not present in the source.

SEQUENTIAL REASONING — Follow these steps internally:
Step 1 — SCAN: Locate passages with specific, factual claims about code, config, decisions, requirements, or explanations.
Step 2 — VERIFY: For each candidate, confirm it is EXPLICITLY stated in the source. Discard any hallucinated or inferred content.
Step 3 — RESOLVE: Make each candidate self-contained. Replace pronouns ("it", "this", "that") and implicit references with the specific entities.
Step 4 — FILTER: Keep only specific, informative, non-obvious facts. Drop trivial statements about conversation flow or common knowledge.
Step 5 — OUTPUT: Produce the JSON below.

CRITICAL — Self-Contained Evidence Rule:
Each evidence sentence MUST be self-contained. Resolve pronouns ("it", "this", "that")
and implicit references. If the evidence refers to a specific concept, file, or
person mentioned in the surrounding context, include that referent explicitly.

Output STRICT JSON:
{
  "extractions": [
    {
      "evidence": "Self-contained factual statement (resolve pronouns)",
      "category": "requirement|decision|explanation|code|reasoning|other",
      "source_context": "Surrounding 1-2 sentences that provide context — helps disambiguate this evidence"
    }
  ]
}

Rules:
- evidence must be directly traceable to the source text
- evidence must be self-contained: "port 8082" → "the Pod B extractor runs on port 8082"
- evidence MUST be a complete, grammatically valid sentence — do NOT output fragments or truncated text
- SIGNIFICANCE: Do NOT extract trivial/obvious statements. Extract only specific, non-obvious, informative facts.
- source_context: include the surrounding sentence(s) that clarify pronouns, references, or conditions
- Extract at least 1 fact if there is meaningful content
- If nothing extractable, return {"extractions": []}
- NOISE DETECTION: If the message is keyboard smash, gibberish, API error message (e.g. "API Error: ECONNRESET", "ConnectionRefused"), or otherwise meaningless text (e.g. "ㅑ다냐졷ㄷ", "asdfasdf"), return {"skip_verdict": "skip"} — do NOT extract facts from noise."""

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


def _calc_timeout(total_chars: int, solo: bool = False) -> int:
    est = TIMEOUT_BASE + int(total_chars * TIMEOUT_PER_CHAR) + GEN_TIME_BUF
    if solo:
        est = int(est * SOLO_TIMEOUT_FACTOR)
    return min(est, 1800)


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
    if not files and not functions:
        return None

    parts = [
        "=== Known Context Entities ==="
        "The following entities were detected in this turn via pattern matching.",
        "",
    ]
    if files:
        parts.append("Files referenced: " + ", ".join(sorted(files)))
    if functions:
        parts.append("Functions referenced: " + ", ".join(sorted(functions)))
    parts.append("")
    parts.append(
        "Use these as grounding references when extracting facts. "
        "If an extracted fact references one of these entities, it is more likely "
        "to be faithful to the source."
    )
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
        timeout = _calc_timeout(len(source_text))

    meta = _call_with_8082_retry(
        call_llm,
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": source_text}],
        model="day_extract",
        max_tokens=MAX_TOKENS_EXTRACT, temperature=TEMP_EXTRACT,
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

    if thinking and len(thinking.strip()) > 5:
        t0 = time.monotonic()
        res = _robust_extract("thinking", thinking)
        if res and res.get("skip"):
            any_skip = True
            print(f"      [thinking] noise skip", flush=True)
        elif res and res.get("extractions"):
            all_extractions.extend(res["extractions"])
            _merge_usage(total_usage, res.get("usage", {}))
            total_elapsed_ms += time.monotonic() - t0
            print(f"      [thinking] {len(res['extractions'])} facts ({time.monotonic()-t0:.0f}s)", flush=True)
        elif res is None:
            print(f"      [thinking section] failed after retries", flush=True)
    time.sleep(6)

    if text:
        t0 = time.monotonic()
        res = _robust_extract("text", text, pulse_context=entity_context)
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

    entity_map = {}
    for t in solo_turns:
        entity_map[t["id"]] = _load_entity_context(t["id"])

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

        warmup_n = min(2, len(targets))
        with ThreadPoolExecutor(max_workers=warmup_n) as pool:
            futs = {}
            for t, src in targets[:warmup_n]:
                ctx = entity_map.get(t["id"]) if section_type == "text" else None
                futs[pool.submit(_extract_section, section_type, src, ctx)] = t
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    res = fut.result()
                    _merge_section(res, t)
                except Exception as e:
                    print(f"  [{section_type}] {t['id'][:8]} warmup failed: {e}", flush=True)

        for t, src in targets[warmup_n:]:
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

    # Phase 2: thinking section
    _batch_extract("thinking", lambda t: (
        (t.get("thinking") or "").strip()
        if len((t.get("thinking") or "").strip()) > 5 else ""
    ))
    if not dry_run:
        for t in solo_turns:
            if turn_data[t["id"]]["extractions"]:
                _save_checkpoint(t["id"], turn_data[t["id"]]["extractions"])
    heartbeat("day_extract", "solo thinking done")
    time.sleep(6)

    # Phase 3: text section
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
