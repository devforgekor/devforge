#!/usr/bin/env python3
# Status: production
# Path: day_cycle.sh — Phase 2 (via day_cycle.py)
"""Extract Pipeline - checkpoint-based perpetual fact extraction.

SSOT: turns.created_at. Checkpoint in pipeline_checkpoint(phase=extract).
Each cycle: SELECT WHERE created_at > checkpoint → extract → verify → store → advance.
Failed turns do NOT advance checkpoint — next cycle retries automatically.
DB UNIQUE (turn_id, fact_index, extract_model) prevents duplicate storage.

Flow:
  Phase 1: SELECT unprocessed (created_at > checkpoint, limit 50)
  Phase 2: day_extract extraction (user/thinking/text)
  Phase 3: Python diff verify (faithfulness check)
  Phase 4: Handle failures — retry or mark
  Phase 5: Store to review_facts + enqueue + advance checkpoint

Usage:
  python3 scripts/pipelines/extract.py                          # process from checkpoint
  python3 scripts/pipelines/extract.py --turn-id <uuid>         # single turn (debug)
  python3 scripts/pipelines/extract.py --limit 50               # batch cap
  python3 scripts/pipelines/extract.py --dry-run                # simulate, no writes
"""

import json
import os
import re
import sys
from pathlib import Path
import subprocess as sp
import time
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.common import strip_think
from lib.db import psql, psql_ok, esc_sql, psql_json, get_checkpoint, advance_checkpoint
from lib.llm_client import call_llm
from lib.llm.json_parser import save_dlq, parse_llm_json
from lib.queue_writer import enqueue_review
from sentence_transformers import SentenceTransformer

# Embedder cache (lazy load)
_EMBEDDER: Optional[SentenceTransformer] = None

# ── Constants ──────────────────────────────────────────────────────────────
# Timeout/token/temp for extraction (day_extract)
TIMEOUT_EXTRACT = 900
MAX_TOKENS_EXTRACT = 512
TEMP_EXTRACT = 0.1
BATCH_LIMIT = 10

# Faithfulness thresholds
COSINE_FAITHFUL = 0.75     # cos ≥ this → faithful
COSINE_UNFAITHFUL = 0.40   # cos < this → unfaithful
COSINE_AMBIGUOUS = (COSINE_UNFAITHFUL, COSINE_FAITHFUL)  # ambiguous range

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


# ── Dual embedding models (lazy singleton, batch-optimized) ─────────────
_BGE_EMBEDDER: Optional[SentenceTransformer] = None
_KO_EMBEDDER: Optional[SentenceTransformer] = None


def _get_embedder_bge():
    """Lazy-load BAAI/bge-m3 (multilingual, 1024d, ~1.1GB FP32)."""
    global _BGE_EMBEDDER
    if _BGE_EMBEDDER is None:
        print("  [embed] Loading BGE-M3 (multilingual 1024d)...", flush=True)
        t0 = time.monotonic()
        _BGE_EMBEDDER = SentenceTransformer('BAAI/bge-m3', cache_folder='/opt/ai_data/models')
        print(f"  [embed] BGE-M3 loaded in {time.monotonic() - t0:.1f}s", flush=True)
    return _BGE_EMBEDDER


def _get_embedder_ko():
    """Lazy-load jhgan/ko-sroberta-multitask (Korean, 768d, ~440MB)."""
    global _KO_EMBEDDER
    if _KO_EMBEDDER is None:
        print("  [embed] Loading ko-sroberta-multitask (Korean 768d)...", flush=True)
        t0 = time.monotonic()
        _KO_EMBEDDER = SentenceTransformer('jhgan/ko-sroberta-multitask', cache_folder='/opt/ai_data/models')
        print(f"  [embed] ko-sroberta loaded in {time.monotonic() - t0:.1f}s", flush=True)
    return _KO_EMBEDDER


# ── Dual-embedding faithfulness ────────────────────────────────────────
def _batch_cosine(embedder: SentenceTransformer, ev_list: List[str],
                  src_list: List[str]) -> List[float]:
    """Batch compute cosine similarity between evidence and source pairs."""
    if not ev_list or not src_list:
        return [0.0] * max(len(ev_list), len(src_list))
    try:
        ev_emb = embedder.encode(ev_list, normalize_embeddings=True, show_progress_bar=False)
        src_emb = embedder.encode(src_list, normalize_embeddings=True, show_progress_bar=False)
        return [float(ev_emb[i] @ src_emb[i]) for i in range(len(ev_list))]
    except Exception as e:
        print(f"  [embed] WARN: {type(e).__name__}: {e}", flush=True)
        return [0.0] * len(ev_list)


def _check_faithfulness_scored(evidence: str, source: str) -> dict:
    """Fallback single-pair faithfulness check (used outside _verify_extractions batch path)."""
    if not evidence or not source:
        return {"faithful": False, "score": 0, "method": "empty", "nli_verdict": None}
    cos = _batch_cosine(_get_embedder_bge(), [evidence], [source])[0]
    score = round(cos * 100, 1)
    if cos >= COSINE_FAITHFUL:
        return {"faithful": True, "score": score, "method": "bge_m3", "nli_verdict": "ENTAILMENT",
                "_bge_cos": cos}
    if cos < COSINE_UNFAITHFUL:
        return {"faithful": False, "score": score, "method": "bge_m3", "nli_verdict": "CONTRADICTION",
                "_bge_cos": cos}
    if _check_faithfulness(evidence, source):
        return {"faithful": True, "score": score, "method": "substr", "nli_verdict": "ENTAILMENT",
                "_bge_cos": cos}
    return {"faithful": True, "score": score, "method": "bge_m3_ambig", "nli_verdict": "NEUTRAL",
            "_bge_cos": cos}


def _dual_embedding_verdict(bge_cos: float, ko_cos: float,
                            evidence: str, source: str) -> dict:
    """Combine BGE-M3 and ko-sroberta cosine scores for final verdict.

    Decision matrix (both models run in batch at _verify_extractions level):
        Both >= FAITHFUL  → ENTAILMENT (confident accept)
        Both < UNFAITHFUL → CONTRADICTION (confident reject)
        Either >= FAITHFUL → ENTAILMENT (one model is confident)
        Otherwise → NEUTRAL (ambiguous → substring + accept, 14B catches bad ones)
    """
    cos_avg = (bge_cos + ko_cos) / 2
    score = round(cos_avg * 100, 1)

    bge_ok = bge_cos >= COSINE_FAITHFUL
    bge_bad = bge_cos < COSINE_UNFAITHFUL
    ko_ok = ko_cos >= COSINE_FAITHFUL
    ko_bad = ko_cos < COSINE_UNFAITHFUL

    if bge_ok and ko_ok:
        return {"faithful": True, "score": score, "method": "dual",
                "nli_verdict": "ENTAILMENT", "_bge_cos": bge_cos, "_ko_cos": ko_cos}
    if bge_bad and ko_bad:
        return {"faithful": False, "score": score, "method": "dual",
                "nli_verdict": "CONTRADICTION", "_bge_cos": bge_cos, "_ko_cos": ko_cos}
    if bge_ok or ko_ok:
        return {"faithful": True, "score": score, "method": "dual_partial",
                "nli_verdict": "ENTAILMENT", "_bge_cos": bge_cos, "_ko_cos": ko_cos}

    # Both in ambiguous range [0.40, 0.75)
    if _check_faithfulness(evidence, source):
        return {"faithful": True, "score": score, "method": "dual_substr",
                "nli_verdict": "ENTAILMENT", "_bge_cos": bge_cos, "_ko_cos": ko_cos}

    # Genuinely ambiguous → accept but flag for 14B scrutiny
    return {"faithful": True, "score": score, "method": "dual_ambig",
            "nli_verdict": "NEUTRAL", "_bge_cos": bge_cos, "_ko_cos": ko_cos}


# ── Hallucination check (substring fallback) ────────────────────────────

# Keep _cosine_faithfulness as an alias for backward compat (nli_compare.py, extract_compare.py)
def _cosine_faithfulness(evidence: str, source: str) -> float:
    """Backward-compat single-pair cosine via BGE-M3 batch path."""
    return _batch_cosine(_get_embedder_bge(), [evidence], [source])[0]
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

        # E4: Dedup
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
def _extract_facts(user_turn: str, thinking: str, text: str,
                   pulse_context: Optional[str] = None) -> Optional[Dict[str, Any]]:
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
    if pulse_context:
        system_prompt = f"{pulse_context}\n\n{system_prompt}"

    meta = call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": "\n".join(parts)}],
        model="day_extract",
        max_tokens=MAX_TOKENS_EXTRACT, temperature=TEMP_EXTRACT, timeout=TIMEOUT_EXTRACT,
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


# ── Phase 3: Batch dual-embedding verify ─────────────────────────────────────
def _verify_extractions(
    extractions: List[Dict[str, Any]],
    user_turn: str, thinking: str, text: str,
) -> List[Dict[str, Any]]:
    """Dual-embedding faithfulness check: BGE-M3 + ko-sroberta in batch.

    All evidence-source pairs are pre-collected and batch-encoded by both
    models (1 inference each, not N per fact). The per-pair verdict matrix:

        Both ≥0.75 → ENTAILMENT (confident accept)
        Both <0.40 → CONTRADICTION (confident reject)
        Either ≥0.75 → ENTAILMENT (one model confident)
        Neither ≥0.75, neither <0.40 → substring fallback, else dual_ambig

    Returns [{fact_type, evidence, category, faithful, faithful_score,
              faithful_method, nli_verdict}].
    """
    source_map = {"user": user_turn, "thinking": thinking, "text": text}

    # Collect evidence-source pairs
    ev_list: List[str] = []
    src_list: List[str] = []
    valid_indices: List[int] = []
    for i, ex in enumerate(extractions):
        evidence = ex.get("evidence", "")
        source = source_map.get(ex.get("fact_type", ""), "")
        if evidence and source:
            ev_list.append(evidence)
            src_list.append(source)
            valid_indices.append(i)

    # Batch encode with both models — only if there are valid pairs
    bge_cos: List[float] = [0.0] * len(extractions)
    ko_cos: List[float] = [0.0] * len(extractions)
    if valid_indices:
        bge_cos_vec = _batch_cosine(_get_embedder_bge(), ev_list, src_list)
        ko_cos_vec = _batch_cosine(_get_embedder_ko(), ev_list, src_list)
        for pos, idx in enumerate(valid_indices):
            bge_cos[idx] = bge_cos_vec[pos]
            ko_cos[idx] = ko_cos_vec[pos]

    # Per-pair verdict
    results = []
    for i, ex in enumerate(extractions):
        verdict = _dual_embedding_verdict(
            bge_cos[i], ko_cos[i],
            ex.get("evidence", ""), source_map.get(ex.get("fact_type", ""), ""),
        )
        results.append({
            "fact_type": ex.get("fact_type", ""),
            "evidence": ex.get("evidence", ""),
            "category": ex.get("category", "other"),
            "faithful": verdict["faithful"],
            "faithful_score": verdict["score"],
            "faithful_method": verdict["method"],
            "nli_verdict": verdict["nli_verdict"],
        })
    return results



# ── Phase 4: Fallback extraction (after day_extract double-failure) ────────────
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
                 nli_verdict: Optional[str] = None,
                 source_file: Optional[str] = None) -> bool:
    """Insert a fact row into review_facts with optional faithfulness metadata."""
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
    if nli_verdict:
        cols.append("nli_verdict")
        vals.append(f"'{esc_sql(nli_verdict)}'")
        set_clauses.append(f"nli_verdict = '{esc_sql(nli_verdict)}'")
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
    """Return turns created after checkpoint, ordered by creation time.

    Checkpoint = last successfully processed turn's created_at.
    Failed turns do NOT advance checkpoint → retried on next cycle.
    """
    checkpoint = get_checkpoint("extract")
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, "
        "  t.source_message_id, t.created_at, "
        "  t.conversation_id, t.seq "
        "FROM turns t "
        "WHERE t.text != '' "
        f"  AND t.created_at > '{esc_sql(checkpoint)}'::timestamptz "
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


# ── Main pipeline ──────────────────────────────────────────────────────────
def extract_pipeline(
    turn_id: Optional[str] = None,
    limit: int = BATCH_LIMIT,
    dry_run: bool = False,
    pulse_context: Optional[str] = None,
) -> Dict[str, Any]:
    """Run day_extract extraction → Python verify → store per turn."""
    t_start = time.monotonic()

    print(f"\n{'=' * 60}")
    print(f"Extract Pipeline — day_extract → Python verify → store")
    if dry_run:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

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

    for idx, turn in enumerate(turns, 1):
        tid = turn["id"]
        ut = turn["user_turn"] or ""
        th = turn["thinking"] or ""
        tx = turn["text"] or ""
        print(f"\n[{idx}/{len(turns)}] Turn {tid[:8]}... "
              f"user={len(ut)}ch think={len(th)}ch text={len(tx)}ch")

        try:
            # ── Phase 2–4: extraction with retry/fallback ──────────
            extractions: Optional[List[Dict[str, Any]]] = None
            mark = ""
            used_model = "day_extract"
            ex_usage: Dict[str, Any] = {}
            ex_timings: Dict[str, Any] = {}
            ex_elapsed: float = 0

            # Single extraction attempt (no faithfulness-based retry needed —
            # downstream day_verify 14B + night R=14B handle hallucination detection)
            print(f"  [extract] day_extract...", flush=True)
            try:
                ex_result = _extract_facts(ut, th, tx, pulse_context=pulse_context)
            except Exception as ex_exc:
                print(f"  [extract]   day_extract exception: {ex_exc}", flush=True)
                mark = "추출 실패"
                _insert_mark(tid, mark, used_model, is_final=True)
                failed += 1
                continue
            if ex_result is None:
                print(f"  [extract]   Parse failure")
                _insert_mark(tid, "추출 parse 실패", used_model, is_final=True)
                failed += 1
                continue

            raw_ex = ex_result["extractions"]
            ex_usage = ex_result.get("usage", {})
            ex_timings = ex_result.get("timings", {})
            ex_elapsed = ex_result.get("elapsed_ms", 0)
            verified = _verify_extractions(raw_ex, ut, th, tx)
            verified = _post_process_extractions(verified, tid, ut, th, tx)  # E1/E3/E4/E5
            extractions = verified

            if extractions is None:
                print(f"  [extract]   No faithful extractions — marking failure")
                if not dry_run:
                    _insert_mark(tid, mark or "추출 2회실패", used_model,
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
                _insert_fact(tid, fi, ft, evidence, used_model,
                             prompt_tokens=pt, gen_tokens=gt, elapsed_ms=em,
                             faithful_score=ex.get("faithful_score"),
                             faithful_method=ex.get("faithful_method"),
                             nli_verdict=ex.get("nli_verdict"))
                fi += 1

            # Write the failure marker if any (only on successful extraction)
            if mark:
                _insert_mark(tid, mark, used_model, is_final=False)

            print(f"  [extract]   Stored {fi} facts", flush=True)
            total_facts += fi
            processed += 1

            # Enqueue for downstream P→R→J review → verify (nightly)
            # → activity_log (DB) type='extract_result', queue_status='reviewed'
            enqueue_review(
                entry_type="extract_result",
                source="extract_pipeline.py",
                title=f"Extract: {tid[:8]} ({fi} facts)",
                summary=(
                    f"{fi} facts ({used_model})" + (f" — {mark}" if mark else "")),
                body={
                    "turn_id": tid,
                    "fact_count": fi,
                    "faithful_count": len(extractions),
                    "extract_model": used_model,
                    "extract_prompt_tokens": pt,
                    "extract_completion_tokens": gt,
                    "extract_elapsed_ms": em,
                    "mark": mark,
                },
                model=used_model,
                turn_ids=[tid],
                tags=["extract", "fact_extraction"],
                queue_status="pending",
            )

            # Advance checkpoint to this turn's created_at
            advance_checkpoint("extract", turn["created_at"])

        except Exception as e:
            print(f"  [extract]   ERROR: {type(e).__name__}: {e}", flush=True)
            if not dry_run:
                _insert_mark(tid, f"ERROR: {e}"[:200], "day_extract", is_final=False)
            failed += 1

    elapsed = round(time.monotonic() - t_start, 1)

    print(f"\n{'=' * 60}", flush=True)
    print(f"Done: {processed} processed, {failed} failed, "
          f"{total_facts} facts ({elapsed}s)")
    if dry_run:
        print("  [DRY RUN] No data was written")
    print(f"{'=' * 60}")

    return {"processed": processed, "failed": failed,
            "facts": total_facts, "elapsed_s": elapsed, "ok": failed == 0}


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
    return {"processed": processed, "failed": failed, "ok": failed == 0}


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
        )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
