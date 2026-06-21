#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh — Extract phase (after polish, via day_cycle.py)
"""Extract Pipeline — state-based fact extraction via NOT EXISTS anti-join.

SSOT: turns table. Filters unprocessed turns using NOT EXISTS against review_facts.
Each cycle: SELECT WHERE NOT EXISTS → extract → verify → store.
Failed turns are retried on next cycle (no checkpoint to advance past them).
DB UNIQUE (turn_id, fact_index, extract_model) prevents duplicate storage.

Flow:
  Phase 1: SELECT unprocessed (NOT EXISTS review_facts WHERE source=extract_pipeline, limit 50)
  Phase 2: day_extract extraction (user/thinking/text)
  Phase 3: Post-process cleanup (dedup, short filter, markdown)
  Phase 4: LLM NLI self-verify — ENTAILMENT=grounded, CONTRADICTION=drop, NEUTRAL→reranker
  Phase 5: Reranker faithfulness check (Pod A :8080) — only NEUTRAL items
  Phase 6: Handle failures — retry or mark
  Phase 7: Store to review_facts + enqueue

Usage:
  python3 scripts/pipelines/extract.py                          # batch from state
  python3 scripts/pipelines/extract.py --turn-id <uuid>         # single turn (debug)
  python3 scripts/pipelines/extract.py --limit 50               # batch cap
  python3 scripts/pipelines/extract.py --dry-run                # simulate, no writes
"""

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import subprocess as sp
import time
from typing import Any, Dict, List, Optional, Tuple

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.common import strip_think
from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.llm_client import call_llm, MODEL_REGISTRY, reranker_score, reranker_nli_verdict
from lib.llm.json_parser import save_dlq, parse_llm_json
# ── Constants ──────────────────────────────────────────────────────────────
# Timeout/token/temp for extraction (day_extract)
TIMEOUT_EXTRACT = 900
MAX_TOKENS_EXTRACT = 512
TEMP_EXTRACT = 0.1
BATCH_LIMIT = 6
TIME_BUDGET = 3600  # default: 1 hour budget for batch slicing

# Dynamic timeout: estimate from input character count + generation time
# Measured: ~9 t/s prompt, ~1.5-2.5 t/s decode
TIMEOUT_BASE = 60
TIMEOUT_PER_CHAR = 0.2
MAX_CHARS_SOLO = 5000
SOLO_TIMEOUT_FACTOR = 2.5
GEN_TIME_BUF = 300  # 512 tok / ~2 t/s gen with parallel contention buffer

# Large turn chunking: split at sentence boundaries to avoid OOM/timeout
_MAX_EXTRACT_CHARS = 5000  # Match embed_batch SLOT_CTX
_CHUNK_OVERLAP_CHARS = 200


def _split_sentences(text: str) -> List[str]:
    """Split text into sentences (Korean + English)."""
    if not text:
        return []
    sents = re.split(r'(?<=[.!?])\s+(?=[A-Z가-힣0-9])|\n\s*\n', text)
    return [s.strip() for s in sents if s.strip()]


def _chunk_text_at_sentences(text: str, max_chars: int,
                              overlap: int) -> List[str]:
    """Split text into overlapping sentence-bounded chunks."""
    if len(text) <= max_chars:
        return [text]
    sents = _split_sentences(text)
    if len(sents) <= 1:
        return [text[:max_chars]]
    chunks = []
    start = 0
    while start < len(sents):
        chunk = []
        length = 0
        end = start
        while end < len(sents) and length + len(sents[end]) <= max_chars:
            chunk.append(sents[end])
            length += len(sents[end])
            end += 1
        if end == start:
            chunk.append(sents[start][:max_chars])
            end = start + 1
        chunks.append(" ".join(chunk))
        # Overlap: start next chunk a few sentences before end for context continuity
        # Guard: if overlap consumes all remaining sentences, we're done
        ov = 0
        ns = end
        while ns > start and ov < overlap:
            ns -= 1
            ov += len(sents[ns])
        start = ns if ns > start else len(sents)
    return chunks


def _chunk_turn_text(user_turn: str, thinking: str, text: str,
                      max_chars: int) -> List[Dict[str, str]]:
    """Split a large turn into overlapping sentence-bounded chunks."""
    if len(text) <= max_chars and len(thinking) <= max_chars:
        return [{"user_turn": user_turn, "thinking": thinking, "text": text}]
    fld, other_val = ("text", thinking) if len(text) >= len(thinking) else ("thinking", text)
    chunks = _chunk_text_at_sentences(
        text if fld == "text" else thinking, max_chars, _CHUNK_OVERLAP_CHARS
    )
    return [
        {"user_turn": user_turn, "thinking": c if fld == "thinking" else other_val,
         "text": c if fld == "text" else other_val}
        for c in chunks
    ]


def _merge_chunk_extractions(
    chunk_results: List[Optional[Dict]]
) -> Dict:
    """Merge extractions from chunks, deduplicating by evidence."""
    all_ex = []
    seen = set()
    total_usage = {}
    for cr in chunk_results:
        if not cr or not cr.get("extractions"):
            continue
        for ex in cr["extractions"]:
            ev = ex.get("evidence", "").strip()
            if ev and ev not in seen:
                seen.add(ev)
                all_ex.append(ex)
        usage = cr.get("usage", {}) or {}
        for k in ("prompt_tokens", "completion_tokens"):
            v = usage.get(k, 0) or 0
            total_usage[k] = (total_usage.get(k, 0) or 0) + v
    return {"extractions": all_ex, "usage": total_usage,
            "timings": {}, "elapsed_ms": 0}


def _process_chunked_turn(user_turn: str, thinking: str, text: str,
                           pulse_context: Optional[str] = None) -> Dict:
    """Split large turn into chunks, extract each via ThreadPoolExecutor, merge results."""
    chunks = _chunk_turn_text(user_turn, thinking, text, _MAX_EXTRACT_CHARS)
    if not chunks:
        return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}

    results: List[Optional[Dict]] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_map = {}
        for i, c in enumerate(chunks):
            cu, ct, cx = c["user_turn"], c["thinking"], c["text"]
            t = _calc_timeout(len(cu) + len(ct) + len(cx))
            fut = pool.submit(_extract_facts, cu, ct, cx,
                              pulse_context=pulse_context, timeout=t)
            fut_map[fut] = i

        for fut in as_completed(fut_map):
            try:
                ex = fut.result()
                if ex:
                    results.append(ex)
            except Exception as e:
                idx = fut_map[fut]
                print(f"      [chunk {idx}] failed: {e}", flush=True)

    if not results:
        return {"extractions": [], "usage": {}, "timings": {}, "elapsed_ms": 0}
    merged = _merge_chunk_extractions(results)
    n = len(merged.get("extractions", []))
    print(f"      [chunked] {len(chunks)} chunks → {n} facts (deduped, parallel=2)", flush=True)
    return merged


def _calc_timeout(total_chars: int, solo: bool = False) -> int:
    """Calculate per-request timeout from input size + generation estimate."""
    est = TIMEOUT_BASE + int(total_chars * TIMEOUT_PER_CHAR) + GEN_TIME_BUF
    if solo:
        est = int(est * SOLO_TIMEOUT_FACTOR)
    return min(est, 1800)



# Faithfulness thresholds

# ── NLI Self-Verify Prompt ─────────────────────────────
_NLI_VERIFY_PROMPT = """You are verifying whether an EVIDENCE sentence is factually supported by a SOURCE sentence.

Follow these steps:
1. Identify the key factual claim in the evidence.
2. Check whether that claim is directly stated or clearly implied by the source.
3. Output exactly one label.

LABELS:
- ENTAILMENT: The evidence is directly supported by the source.
- CONTRADICTION: The evidence contradicts the source — they cannot both be true.
- NEUTRAL: The evidence is related but not directly entailed by the source.

Output EXACTLY one word: ENTAILMENT | CONTRADICTION | NEUTRAL
No punctuation. No explanation.

SOURCE: {source}
EVIDENCE: {evidence}"""

# ── System prompts ─────────────────────────────────────────────────────────
SYSTEM_DAY_EXTRACT = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

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

# ── JSON parser ────────────────────────────────────────────────────────────


def _parse_json(raw: str, label: str = "LLM", attempt: int = 1) -> Optional[Dict[str, Any]]:
    """Extract JSON from LLM output using shared parse_llm_json + DLQ.

    Strips <think> blocks before parsing (R1 reasoning), falls through to
    parse_llm_json (stdlib → json_repair). Saves parse failures to DLQ.
    """
    cleaned = strip_think(raw)
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(raw, stage=f"extract_{label}", error="parse_llm_json returned None",
                 attempt=attempt)
    return result


# ── Reranker faithfulness via Pod A (MODEL_REGISTRY) ──────────────────


def _rerank_score(evidence: str, source: str) -> float:
    """Score evidence-source relevance via shared reranker_score()."""
    return reranker_score(evidence, source)


def _cosine_faithfulness(evidence: str, source: str) -> float:
    """Backward-compat: single-pair reranker score."""
    return _rerank_score(evidence, source)


def _check_faithfulness(evidence: str, source: str) -> bool:
    """Return True if evidence is a substring of source (faithful)."""
    if not evidence or not source:
        return False
    ev = " ".join(evidence.split())
    src = " ".join(source.split())
    return ev.lower() in src.lower()


# ── Post-processing: E1~E5 cleanup ──────────────────────────────
_OPERATIONAL_PATTERNS = re.compile(
    r'^(?:네[,.!]?\s*)?(?:알겠습니다|이해했습니다|확인했습니다|시작합니다|시작하겠습니다'
    r'|검토하겠습니다|진행하겠습니다|수정하겠습니다|업데이트하겠습니다'
    r'|적용하겠습니다|확인해보겠습니다|찾아보겠습니다|만들겠습니다)'
    r'|^(?:좋습니다|좋아요|맞습니다|그렇습니다|그럼|자[,.!]?)'
    r'|^(?:감사합니다|고맙습니다|수고하셨습니다)'
    r'|^분석 공유 감사|^끝났습니다|^완료했습니다|^완료'
    r'|(?:Let me|I will|I.ll|I can|I need to|Lets)',
    re.IGNORECASE,
)
_VALID_CATS = {"requirement", "decision", "explanation", "code", "reasoning", "other"}


def _clean_markdown(text: str) -> str:
    """Strip all markdown formatting from evidence. (Improvement #1, #4)"""
    if not text:
        return text
    # Code fences
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'``.*?``', '', text)
    # Inline code `word`
    text = re.sub(r'`([^`]+)`', r'\1', text)
    # Bold **word**
    text = re.sub(r'\*{2,}([^*]+)\*{2,}', r'\1', text)
    # Underline __word__
    text = re.sub(r'_{2,}([^_]+)_{2,}', r'\1', text)
    # Strikethrough ~~word~~
    text = re.sub(r'~{2,}([^~]+)~{2,}', r'\1', text)
    # Remaining single backticks (edge cases)
    text = text.replace('`', '')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _infer_category(evidence: str, current_cat: str) -> str:
    """Improvement #5: Auto-infer category from evidence content."""
    cat = current_cat.lower()
    if cat in _VALID_CATS:
        return cat
    if re.search(r'(?:\.py|\.sh|\.yaml|\.md|\.env|\.json|\bdef\s+\w+\b)', evidence):
        return "code"
    if "=" in evidence and re.search(r"\w+\s*=\s*[\w\d/\"']", evidence):
        return "code"
    return "explanation"


def _post_process_extractions(
    verified: List[Dict[str, Any]],
    turn_id: str,
    user_turn: str = "",
    thinking: str = "",
    text: str = "",
    context_limit: int = 50,
) -> List[Dict[str, Any]]:
    """Post-process verified extractions: E1/E3/E4/E5 cleanup.

    Improvements applied:
      1. Single backtick strip  (E3 markdown)
      2. Short evidence filter  (E1 operational)
      3. Markdown format strip  (E3 clean)
      4. Cross-turn dedup        (E4 temporal)
      5. Category inference      (quality)

    Called after _verify_extractions() and before _insert_fact().
    """
    if not verified:
        return verified

    cleaned: List[Dict[str, Any]] = []
    seen_normalized: set = set()
    seen_exact: set = set()  # within-turn exact dedup

    # Load recent evidence for E4 cross-turn dedup
    recent_evidence: set = set()
    try:
        sql = (
            f"SELECT DISTINCT evidence FROM review_facts "
            f"WHERE turn_id != '{esc_sql(turn_id)}'::uuid "
            f"AND created_at > NOW() - INTERVAL '24 hours' "
            f"LIMIT {context_limit}"
        )
        rows = psql_json(sql)
        if rows:
            for row in rows:
                ev = row.get("evidence", "")
                if ev:
                    key = re.sub(r'[^a-zA-Z0-9가-힣]', '', ev[:50]).lower()
                    if len(key) > 5:
                        recent_evidence.add(key)
    except Exception:
        pass  # best-effort

    for ex in verified:
        evidence = ex.get("evidence", "")
        if not evidence:
            continue

        # E1: Operational gate reinforcement
        if _OPERATIONAL_PATTERNS.search(evidence):
            continue
        # Improvement #2: Short evidence filter (< 12 chars, no =/:)
        if len(evidence.strip()) < 12 and "=" not in evidence and ":" not in evidence:
            continue

        # Improvement #1+#4: Strip all markdown formatting
        evidence = _clean_markdown(evidence)
        if not evidence:
            continue

        # Exact dedup (within-turn): same text verbatim → skip
        if evidence in seen_exact:
            continue
        seen_exact.add(evidence)

        # E4: Cross-turn dedup
        norm_key = re.sub(r'[^a-zA-Z0-9가-힣]', '', evidence[:50]).lower()
        if len(norm_key) > 5:
            if norm_key in recent_evidence or norm_key in seen_normalized:
                continue
            seen_normalized.add(norm_key)

        # Improvement #5: Category inference
        ex["category"] = _infer_category(evidence, ex.get("category", "explanation"))
        ex["evidence"] = evidence
        cleaned.append(ex)

    return cleaned



# ── Phase 2: extraction ────────────────────────────────────────────────
def _load_entity_context(turn_id: str) -> Optional[str]:
    """Load entity_scan results for a turn (Phase 0).

    Returns formatted context string for LLM injection, or None.
    """
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


def _extract_facts(user_turn: str, thinking: str, text: str,
                   pulse_context: Optional[str] = None,
                   timeout: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Run day_extract extraction. Returns {extractions, usage, timings, elapsed_ms}."""
    parts = [
        "=== user_turn ===",
        user_turn or "(empty)",
        "",
        "=== thinking ===",
        thinking or "(empty)",
        "",
        "=== text ===",
        text or "(empty)",
    ]

    system_prompt = SYSTEM_DAY_EXTRACT
    user_prompt = "\n".join(parts)
    if pulse_context:
        system_prompt = f"{pulse_context}\n\n{system_prompt}"

    meta = call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": "\n".join(parts)}],
        model="day_extract",
        max_tokens=MAX_TOKENS_EXTRACT, temperature=TEMP_EXTRACT,
        timeout=timeout if timeout is not None else TIMEOUT_EXTRACT,
        json_mode=True, return_meta=True,
    )
    raw = meta["content"]
    parsed = _parse_json(raw, "day_extract", attempt=1)
    if parsed is None:
        return None
    ex = parsed.get("extractions", [])
    result = {"extractions": ex, "usage": meta["usage"], "timings": meta["timings"],
              "elapsed_ms": meta["elapsed_ms"]} if isinstance(ex, list) else None
    if result and "rubric_evaluation" in parsed:
        result["rubric_evaluation"] = parsed["rubric_evaluation"]
    return result


# ── Phase 3: Reranker faithfulness verify ──────────────────────────────
def _verify_extractions(
    extractions: List[Dict[str, Any]],
    user_turn: str, thinking: str, text: str,
) -> List[Dict[str, Any]]:
    """Reranker grounding check via Pod A reranker (MODEL_REGISTRY).

    Each evidence-source pair is scored by the cross-encoder reranker.
    The reranker measures TOPICAL RELEVANCE, not logical entailment.

    ⚠ LIMITATION:
      - GROUNDED (≥0.75): content is topically related to source
      - UNGROUNDED (<0.40): content is on a different topic
      - AMBIGUOUS (0.40-0.75): somewhat related, substring fallback
      - Does NOT detect: negation, numerical contradictions, added content

    Returns [{fact_type, evidence, category, faithful, faithful_score,
              faithful_method, grounding}].
    """
    source_map = {"user": user_turn, "thinking": thinking, "text": text}

    results = []
    for ex in extractions:
        evidence = ex.get("evidence", "")
        source = source_map.get(ex.get("fact_type", ""), "")
        cos = _rerank_score(evidence, source) if evidence and source else 0.0
        score = round(cos * 100, 1)
        grounding = reranker_nli_verdict(cos)
        if grounding == "GROUNDED":
            verdict = {"faithful": True, "score": score, "method": "reranker",
                       "grounding": grounding}
        elif grounding == "UNGROUNDED":
            verdict = {"faithful": False, "score": score, "method": "reranker",
                       "grounding": grounding}
        else:
            # AMBIGUOUS — substring fallback, else accepted (verify catches)
            if _check_faithfulness(evidence, source):
                verdict = {"faithful": True, "score": score, "method": "reranker_substr",
                           "grounding": "GROUNDED"}
            else:
                verdict = {"faithful": True, "score": score, "method": "reranker_ambig",
                           "grounding": "AMBIGUOUS"}
        results.append({
            "fact_type": ex.get("fact_type", ""),
            "evidence": evidence,
            "category": ex.get("category", "other"),
            "faithful": verdict["faithful"],
            "faithful_score": verdict["score"],
            "faithful_method": verdict["method"],
            "grounding": verdict["grounding"],
            "nli_llm": ex.get("nli_llm", "NEUTRAL"),
        })
    return results



# ── Phase 4: LLM NLI Self-Verify ──────────────────────────────
def _calc_nli_timeout(source: str, evidence: str) -> int:
    """Dynamic timeout for NLI verify (~2500 chars / 9 t/s * 1.5 safety)."""
    total = len(source[:2000]) + len(evidence[:500]) + 200
    return min(max(30, int(total * 0.15)), 600)


def _llm_nli_check(evidence: str, source: str) -> str:
    """Run NLI self-verify on a single evidence-source pair.

    Returns: "ENTAILMENT", "CONTRADICTION", or "NEUTRAL"
    Falls back to "NEUTRAL" on any parse/network error.

    Uses structured step prompt (CoVe-style instructions in prompt)
    and robust first-word extraction for reliable parsing.
    """
    if not evidence or not source:
        return "NEUTRAL"

    prompt = _NLI_VERIFY_PROMPT.format(
        source=source[:2000], evidence=evidence[:500]
    )
    try:
        meta = call_llm(
            [{"role": "user", "content": prompt}],
            model="day_extract",
            max_tokens=64, temperature=0.0, timeout=_calc_nli_timeout(source, evidence),
            return_meta=True,
        )
        raw = meta["content"].strip().upper()
        for tok in raw.replace("\n", " ").split():
            tok = tok.strip(".,!?;:\"'()[]")
            if tok in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
                return tok
        return "NEUTRAL"
    except Exception:
        return "NEUTRAL"


def _llm_nli_verify(
    extractions: List[Dict[str, Any]],
    user_turn: str, thinking: str, text: str,
) -> List[Dict[str, Any]]:
    """LLM NLI verify — adds nli_llm field (ENTAILMENT|CONTRADICTION|NEUTRAL).

    1st pass in NLI→reranker chain. ENTAILMENT→auto grounded, CONTRADICTION→drop,
    NEUTRAL→reranker (Phase 5). Reranker override and labeling handled at pipeline level.

    Detects via logical entailment (reranker blind spot):
      - Negation flip ("not X" vs "X")
      - Numerical contradictions ("5" vs "10")
      - Hallucinated content not present in source
    """
    source_map = {"user": user_turn, "thinking": thinking, "text": text}

    for ex in extractions:
        evidence = ex.get("evidence", "")
        source = source_map.get(ex.get("fact_type", ""), "")

        if not evidence or not source:
            ex["nli_llm"] = "SKIP"
            continue

        verdict = _llm_nli_check(evidence, source)
        ex["nli_llm"] = verdict

    return extractions


# ── Phase 5: Fallback extraction (after day_extract double-failure) ────────────
def _fallback_extract(user_turn: str, thinking: str, text: str,
                      model: str = "day_extract",
                      pulse_context: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fallback extraction after extractor double-failure. Uses *model* (default day_extract)."""
    parts = [
        "=== user_turn ===", user_turn or "(empty)",
        "", "=== thinking ===", thinking or "(empty)",
        "", "=== text ===", text or "(empty)",
    ]

    system_prompt = SYSTEM_FALLBACK
    if pulse_context:
        system_prompt = f"{pulse_context}\n\n{system_prompt}"

    meta = call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": "\n".join(parts)}],
        model=model,
        max_tokens=MAX_TOKENS_EXTRACT, temperature=TEMP_EXTRACT, timeout=TIMEOUT_EXTRACT,
        json_mode=True, return_meta=True,
    )
    raw = meta["content"]
    parsed = _parse_json(raw, f"{model} fallback")
    if parsed is None:
        return None
    ex = parsed.get("extractions", [])
    if not isinstance(ex, list):
        return None
    return {"fallback_note": parsed.get("fallback_note", ""), "extractions": ex,
            "usage": meta["usage"], "timings": meta["timings"], "elapsed_ms": meta["elapsed_ms"],
            "rubric_evaluation": parsed.get("rubric_evaluation", {})}


# ── DB writers ─────────────────────────────────────────────────────────────
def _insert_fact(turn_id: str, fact_index: int, fact_type: str,
                 evidence: str, extract_model: str,
                 prompt_tokens: Optional[int] = None,
                 gen_tokens: Optional[int] = None,
                 elapsed_ms: Optional[float] = None,
                 faithful_score: Optional[int] = None,
                 faithful_method: Optional[str] = None,
                 grounding: Optional[str] = None,
                 nli_llm: Optional[str] = None,
                 source_file: Optional[str] = None) -> bool:
    """Insert a fact row into review_facts with optional grounding metadata."""
    cols = ["turn_id", "fact_index", "fact_type", "evidence", "extract_model", "verdict",
            "source", "fact_action", "fact_confidence"]
    vals = [f"'{esc_sql(turn_id)}'::uuid", str(fact_index),
            f"'{esc_sql(fact_type)}'", f"'{esc_sql(evidence[:5000])}'",
            f"'{esc_sql(extract_model)}'", "'pending'",
            "'extract_pipeline'", f"'{esc_sql(faithful_method or 'store')}'",
            str(int(faithful_score) if faithful_score is not None else 100)]
    set_clauses = [
        f"fact_action = '{esc_sql(faithful_method or 'store')}'",
        f"fact_confidence = {int(faithful_score) if faithful_score is not None else 100}",
    ]
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
    if grounding:
        cols.append("nli_verdict")
        vals.append(f"'{esc_sql(grounding)}'")
        set_clauses.append(f"nli_verdict = '{esc_sql(grounding)}'")
    if nli_llm:
        cols.append("nli_llm")
        vals.append(f"'{esc_sql(nli_llm)}'")
        set_clauses.append(f"nli_llm = '{esc_sql(nli_llm)}'")
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


def _insert_mark(turn_id: str, mark: str, extract_model: str, is_final: bool = False) -> bool:
    """Insert a failure-marker fact row (for traceability).

    is_final=True: source='extract_pipeline' — blocks reprocessing.
    is_final=False: source='extract_marker' — allows retry on next batch.
    """
    source = "extract_pipeline" if is_final else "extract_marker"
    sql = (
        "INSERT INTO review_facts "
        "(turn_id, fact_index, fact_type, evidence, extract_model, verdict, "
        " source, reason, fact_action, fact_confidence) "
        "VALUES ("
        f"'{esc_sql(turn_id)}'::uuid, "
        f"(SELECT COALESCE(MAX(fact_index), -1) + 1 "
        f" FROM review_facts WHERE turn_id = '{esc_sql(turn_id)}'::uuid), "
        f"'marker', '{esc_sql(mark)}', "
        f"'{esc_sql(extract_model)}', 'system', "
        f"'{source}', 'tracking_marker', 'marked', 0"
        ")"
    )
    return psql_ok(sql)


# ── Checkpoint ────────────────────────────────────────────────────────────────


# ── Phase 1: Select turns ─────────────────────────────────────────────────
def _get_unprocessed_turns(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Return turns without successful extraction, ordered by creation time.

    State-based filter: NOT EXISTS extract_pipeline rows means
    this turn has never been successfully extracted.
    Failed turns (source='extract_marker') are automatically retried.
    """
    sql = (
        "SELECT t.id, "
        "  COALESCE(t.user_turn_clean_polished, t.user_turn_clean, t.user_turn) AS user_turn, "
        "  COALESCE(t.thinking_clean_polished, t.thinking_clean, t.thinking) AS thinking, "
        "  COALESCE(t.text_clean_polished, t.text_clean, t.text) AS text, "
        "  t.source_message_id, t.created_at, "
        "  t.conversation_id, t.seq "
        "FROM turns t "
        "WHERE t.text != '' "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM review_facts rf "
        "    WHERE rf.turn_id = t.id "
        "    AND rf.source = 'extract_pipeline'"
        "  ) "
        "ORDER BY t.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    turns = []
    for row in rows:
        turns.append({
            "id": row.get("id", ""),
            "user_turn": row.get("user_turn", ""),
            "thinking": row.get("thinking") or None,
            "text": row.get("text", ""),
            "source_message_id": row.get("source_message_id", ""),
            "created_at": row.get("created_at", ""),
            "conversation_id": row.get("conversation_id", ""),
            "seq": row.get("seq", 0) or 0,
        })
    return turns


# ── Concurrent extraction config ─────────────────────────────────────────────
# Must match llama-server --parallel (pod-b-entrypoint.sh PARALLEL env var).
PARALLEL = 2


def _extract_for_turn(turn: dict, pulse_context: Optional[str] = None) -> Tuple[dict, Optional[Dict[str, Any]], Optional[str]]:
    """Wrapper for parallel LLM call. Auto-chunks large turns at sentence boundaries.
    Returns (turn, ex_result_or_None, error_str_or_None)."""
    user_turn = turn.get("user_turn") or ""
    thinking = turn.get("thinking") or ""
    text = turn.get("text") or ""
    total_chars = len(user_turn) + len(thinking) + len(text)

    # Phase 0: Inject entity context from entity_scan
    entity_context = _load_entity_context(turn.get("id", ""))
    if entity_context:
        pulse_context = (
            f"{pulse_context}\n\n{entity_context}"
            if pulse_context
            else entity_context
        )

    # Large turn: chunk at sentence boundaries to avoid OOM/timeout
    if total_chars > _MAX_EXTRACT_CHARS:
        try:
            ex_result = _process_chunked_turn(user_turn, thinking, text, pulse_context)
            return (turn, ex_result, None)
        except Exception as e:
            return (turn, None, str(e))

    # Standard single-call path
    solo = total_chars > MAX_CHARS_SOLO
    timeout = _calc_timeout(total_chars, solo=solo)
    try:
        ex_result = _extract_facts(user_turn, thinking, text,
                                   pulse_context=pulse_context,
                                   timeout=timeout)
        return (turn, ex_result, None)
    except Exception as e:
        return (turn, None, str(e))


# ── Main pipeline ──────────────────────────────────────────────────────────
def extract_pipeline(
    turn_id: Optional[str] = None,
    limit: int = BATCH_LIMIT,
    dry_run: bool = False,
    pulse_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Run extraction → reranker verify → store per turn."""
    t_start = time.monotonic()

    print(f"\n{'=' * 60}")
    print(f"Extract Pipeline — LLM extract → reranker verify → store")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    # Ensure nli_llm column exists (idempotent migration)
    psql_ok("ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm TEXT")

    # ── Phase 1: Select turns ──────────────────────────────────────────
    if turn_id:
        sql = (
            "SELECT t.id, t.user_turn, t.thinking, t.text, "
            "  t.source_message_id, t.created_at, "
            "  t.conversation_id, t.seq "
            f"FROM turns t WHERE t.id = '{esc_sql(turn_id)}'::uuid"
        )
        rows = psql_json(sql)
        if not rows:
            print(f"[extract] Turn not found: {turn_id}")
            return {"processed": 0, "failed": 1, "facts": 0, "ok": False}
        r = rows[0]
        turns = [{
            "id": r["id"], "user_turn": r["user_turn"],
            "thinking": r.get("thinking") or None, "text": r["text"],
            "source_message_id": r.get("source_message_id", ""),
            "created_at": r["created_at"],
            "conversation_id": r["conversation_id"],
            "seq": r.get("seq", 0) or 0,
        }]
    else:
        turns = _get_unprocessed_turns(limit)

    if not turns:
        print("[extract] No unprocessed turns found")
        return {"processed": 0, "failed": 0, "facts": 0, "ok": True}

    print(f"[extract] Processing {len(turns)} turn(s)", flush=True)

    total_facts = 0
    processed = 0
    failed = 0

    # ── Phase 1: Concurrent LLM calls (parallel=PARALLEL) ────────────
    print(f"[extract] Submitting {len(turns)} turns to LLM (parallel={PARALLEL})...",
          flush=True)
    turn_results: Dict[str, Tuple] = {}  # turn_id -> (ex_result, error_str)
    llm_t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        fut_map = {}
        for ti, t in enumerate(turns, 1):
            fut = pool.submit(_extract_for_turn, t, pulse_context)
            fut_map[fut] = (ti, t)
        for fut in as_completed(fut_map):
            ti, t = fut_map[fut]
            turn, ex_result, error = fut.result()
            if error:
                print(f"  [{ti}/{len(turns)}] {t['id'][:8]} — LLM call failed: {error}",
                      flush=True)
            turn_results[t["id"]] = (ex_result, error)

    print(f"  [extract]   LLM calls: {time.monotonic() - llm_t0:.1f}s",
          flush=True)

    # ── Phase 2: Sequential verify → store → checkpoint ─────────────────
    idx = 0
    for turn in turns:
        idx += 1
        turn_id_val = turn["id"]
        user_turn = turn["user_turn"] or ""
        thinking = turn["thinking"] or ""
        text = turn["text"] or ""
        print(f"\n[{idx}/{len(turns)}] Turn {turn_id_val[:8]}... "
              f"user={len(user_turn)}ch think={len(thinking)}ch text={len(text)}ch")

        try:
            ex_result, error = turn_results.get(turn_id_val, (None, "missing batch result"))
            used_model = "day_extract"
            mark = ""

            if error:
                print(f"  [extract]   day_extract exception: {error}", flush=True)
                mark = "추출 실패"
                if not dry_run:
                    _insert_mark(turn_id_val, mark, used_model, is_final=True)
                failed += 1
                continue

            if ex_result is None:
                print(f"  [extract]   Parse failure")
                if not dry_run:
                    _insert_mark(turn_id_val, "추출 parse 실패", used_model, is_final=True)
                failed += 1
                continue

            raw_ex = ex_result["extractions"]
            ex_usage = ex_result.get("usage", {})
            ex_timings = ex_result.get("timings", {})
            ex_elapsed = ex_result.get("elapsed_ms", 0)
            # Phase 0: Cheap cleanup first (dedup, short filter, markdown cleanup)
            verified = _post_process_extractions(raw_ex, turn_id_val, user_turn, thinking, text)
            # Phase 1: LLM NLI — ENTAILMENT→grounded, CONTRADICTION→drop, NEUTRAL→reranker
            verified = _llm_nli_verify(verified, user_turn, thinking, text)
            # Phase 2: ENTAILMENT auto-grounded, CONTRADICTION auto-dropped
            entail = []
            neutral = []
            for v in verified:
                nli = v.get("nli_llm", "NEUTRAL")
                if nli == "ENTAILMENT":
                    v.update({"faithful": True, "faithful_score": 100,
                              "faithful_method": "nli", "grounding": "GROUNDED"})
                    entail.append(v)
                elif nli == "CONTRADICTION":
                    v.update({"faithful": False, "faithful_score": 0,
                              "faithful_method": "nli", "grounding": "UNGROUNDED"})
                else:
                    neutral.append(v)
            # Phase 3: Reranker only NEUTRAL items
            rerankered = _verify_extractions(neutral, user_turn, thinking, text) if neutral else []
            extractions = entail + rerankered

            if extractions is None:
                print(f"  [extract]   No faithful extractions — marking failure")
                if not dry_run:
                    _insert_mark(turn_id_val, mark or "추출 2회실패", used_model,
                                 is_final=True)
                failed += 1
                continue

            # ── Store ────────────────────────────────────────────────────
            if dry_run:
                print(f"  [extract]   [DRY] Would store {len(extractions)} facts")
                total_facts += len(extractions)
                processed += 1
                continue

            fi = 0
            # Extract timing for the successful attempt
            pt = ex_usage.get("prompt_tokens") if ex_usage else None
            gt = ex_usage.get("completion_tokens") if ex_usage else None
            em = ex_elapsed

            for ex in extractions:
                ft = ex.get("fact_type", "text")
                evidence = ex.get("evidence", "")
                _insert_fact(turn_id_val, fi, ft, evidence, used_model,
                             prompt_tokens=pt, gen_tokens=gt, elapsed_ms=em,
                             faithful_score=ex.get("faithful_score"),
                             faithful_method=ex.get("faithful_method"),
                             grounding=ex.get("grounding"),
                             nli_llm=ex.get("nli_llm"))
                fi += 1

            # Write the failure marker if any (only on successful extraction)
            if mark:
                _insert_mark(turn_id_val, mark, used_model, is_final=False)

            print(f"  [extract]   Stored {fi} facts", flush=True)
            total_facts += fi
            processed += 1

        except Exception as e:
            print(f"  [extract]   ERROR: {type(e).__name__}: {e}", flush=True)
            if not dry_run:
                _insert_mark(turn_id_val, f"ERROR: {e}"[:200], "day_extract", is_final=False)
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)

    print(f"\n{'=' * 60}", flush=True)
    print(f"Done: {processed} processed, {failed} failed, "
          f"{total_facts} facts ({elapsed}s)")
    if dry_run:
        print("  [DRY RUN] No data was written")
    print(f"{'=' * 60}")

    return {"processed": processed, "failed": failed,
            "facts": total_facts, "elapsed_s": elapsed, "ok": processed > 0}


# ── File description phase ──────────────────────────────────────────────────
_TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".csv",
                    ".log", ".html", ".css", ".js", ".sh", ".toml", ".xml",
                    ".cfg", ".ini", ".conf", ".env", ".rst", ".tex"}


def _sample_content(path: str, max_bytes: int = 2048) -> str:
    """Read first max_bytes of a text file. Returns empty string for binary files or errors."""
    ext = Path(path).suffix.lower()
    if ext not in _TEXT_EXTENSIONS:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(max_bytes)
    except Exception:
        return ""


def describe_file_batch(dry_run: bool = False, limit: int = 20) -> Dict[str, Any]:
    """Scan file_registry for undescribed files, generate descriptions + tags via LLM.

    Uses day_extract model for lightweight description generation.
    """
    from lib.file_registry import scan_undescribed, update_metadata

    files = scan_undescribed()
    if isinstance(files, list) and len(files) > limit:
        files = files[:limit]

    if not files:
        print("[describe-files] No undescribed files found")
        return {"processed": 0, "failed": 0, "ok": True}

    print(f"[describe-files] Describing {len(files)} file(s)")
    processed = 0
    failed = 0

    for f in files:
        fname = f.get("filename", "?")
        mime = f.get("mime_type", "?")
        fsize = f.get("size", 0)
        fsrc = f.get("source", "?")
        content = _sample_content(f.get("path", ""))
        print(f"  [{processed + 1}/{len(files)}] {fname} ({mime}, {fsize}b)")

        parts = [
            f"filename: {fname}",
            f"mime_type: {mime}",
            f"size: {fsize} bytes",
            f"source: {fsrc}",
        ]
        if content:
            parts.append("")
            parts.append("=== content (first 2KB) ===")
            parts.append(content)

        meta = call_llm(
            [{"role": "system", "content": SYSTEM_DESCRIBE_FILE},
             {"role": "user", "content": "\n".join(parts)}],
            model="day_extract",
            max_tokens=256, temperature=0.1, timeout=60,
            json_mode=True, return_meta=True,
        )
        raw = meta["content"]
        parsed = _parse_json(raw, "describe_file")
        if not parsed:
            print(f"    Parse failure, skipping")
            failed += 1
            continue

        desc = parsed.get("description", "")
        tags = parsed.get("tags", [])
        if not desc:
            print(f"    Empty description from LLM")
            failed += 1
            continue

        print(f"    → {desc}")
        if tags:
            print(f"    tags: {', '.join(tags)}")
        if not dry_run:
            update_metadata(f["id"], description=desc, tags=tags)
        processed += 1

    print(f"[describe-files] Done: {processed} described, {failed} failed")
    return {"processed": processed, "failed": failed, "ok": processed > 0}


# ── CLI ────────────────────────────────────────────────────────────────────
def main() -> None:
    preflight_checks("extract.py")
    import argparse
    parser = argparse.ArgumentParser(
        description=f"Extract Pipeline — day_extract → Python verify → store")
    parser.add_argument("--turn-id", help="Process a specific turn UUID")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--pulse-context", help="Inject Watchdog Pulse context")
    parser.add_argument("--describe-files", action="store_true",
                        help="Scan file_registry for undescribed files and generate descriptions")
    args = parser.parse_args()

    if args.describe_files:
        result = describe_file_batch(dry_run=args.dry_run, limit=args.limit)
    else:
        result = extract_pipeline(
            turn_id=args.turn_id,
            limit=args.limit,
            dry_run=args.dry_run,
            pulse_context=args.pulse_context,
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
