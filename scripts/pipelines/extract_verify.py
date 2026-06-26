#!/usr/bin/env python3
# Status: production
# Path: extract.py — submodule for verification and post-processing
"""Verification submodule for the Extract Pipeline.

Contains NLI self-verify, reranker faithfulness, post-processing cleanup,
batch refinement, and fallback extraction logic.
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import esc_sql, psql_json
from lib.llm_client import call_llm, reranker_score, reranker_nli_verdict
from lib.watchdog.messenger import heartbeat
from lib.common import context_limit


# ── NLI Self-Verify Prompt ──────────────────────────────────────

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


# ── Incomplete Fact Refinement prompt ───────────────────────────

_REFINE_FACT_PROMPT = """Given an evidence sentence that may be incomplete, and a source context providing additional detail:

Evidence: {evidence}
Source: {source_context}

Task: Refine the evidence to be complete and self-contained by incorporating relevant information from the source context. Follow these rules:
1. Stay strictly faithful to the source context — do NOT add facts not present in the source
2. Keep it concise (1-3 sentences)
3. Output ONLY the refined evidence text, nothing else"""


# ── Post-processing constants ───────────────────────────────────

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


# ── Post-processing functions ───────────────────────────────────


def _clean_markdown(text: str) -> str:
    if not text:
        return text
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'``.*?``', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\*{2,}([^*]+)\*{2,}', r'\1', text)
    text = re.sub(r'_{2,}([^_]+)_{2,}', r'\1', text)
    text = re.sub(r'~{2,}([^~]+)~{2,}', r'\1', text)
    text = text.replace('`', '')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _infer_category(evidence: str, current_cat: str) -> str:
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
    if not verified:
        return verified

    cleaned: List[Dict[str, Any]] = []
    seen_normalized: set = set()
    seen_exact: set = set()

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
        pass

    for ex in verified:
        evidence = ex.get("evidence", "")
        if not evidence:
            continue

        if _OPERATIONAL_PATTERNS.search(evidence):
            continue
        if len(evidence.strip()) < 12 and "=" not in evidence and ":" not in evidence:
            continue

        evidence = _clean_markdown(evidence)
        if not evidence:
            continue

        if evidence in seen_exact:
            continue
        seen_exact.add(evidence)

        norm_key = re.sub(r'[^a-zA-Z0-9가-힣]', '', evidence[:50]).lower()
        if len(norm_key) > 5:
            if norm_key in recent_evidence or norm_key in seen_normalized:
                continue
            seen_normalized.add(norm_key)

        ex["category"] = _infer_category(evidence, ex.get("category", "explanation"))
        ex["evidence"] = evidence
        cleaned.append(ex)

    return cleaned


# ── Incomplete Fact Refinement ──────────────────────────────────


def _refine_batch(
    extractions_by_turn: Dict[str, List[Dict]],
) -> None:
    candidates = []
    for tid, extractions in extractions_by_turn.items():
        for i, ex in enumerate(extractions):
            if len(ex.get("source_context", "") or "") > 10:
                candidates.append((tid, i, ex["source_context"][:500],
                                   ex.get("evidence", "")[:300]))

    if not candidates:
        return

    def _refine_one(cand):
        tid, idx, src, ev = cand
        try:
            reply = call_llm(
                [{"role": "user", "content": _REFINE_FACT_PROMPT.format(evidence=ev, source_context=src)}],
                model="day_extract", max_tokens=256, temperature=0.0, timeout=60,
            )
            refined = reply.strip().strip('"\'')
            if len(refined) > 10 and refined != ev:
                return (tid, idx, refined)
        except Exception:
            pass
        return (tid, idx, f"{ev} | {src}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_refine_one, candidates))

    for tid, idx, corrected in results:
        if tid in extractions_by_turn and idx < len(extractions_by_turn[tid]):
            extractions_by_turn[tid][idx]["corrected_evidence"] = corrected
            print(f"      [refine] turn {tid[:8]} fact {idx}: refined ({len(corrected)}ch)", flush=True)


# ── Reranker faithfulness ───────────────────────────────────────


def _rerank_score(evidence: str, source: str) -> float:
    return reranker_score(evidence, source)


def _cosine_faithfulness(evidence: str, source: str) -> float:
    return _rerank_score(evidence, source)


def _check_faithfulness(evidence: str, source: str) -> bool:
    if not evidence or not source:
        return False
    ev = " ".join(evidence.split())
    src = " ".join(source.split())
    return ev.lower() in src.lower()


def _verify_extractions(
    extractions: List[Dict[str, Any]],
    user_turn: str, thinking: str, text: str,
) -> List[Dict[str, Any]]:
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
        elif grounding == "RERANKER_ERROR":
            verdict = {"faithful": False, "score": score, "method": "reranker_err",
                       "grounding": "RERANKER_ERROR"}
        else:
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


# ── LLM NLI Self-Verify ─────────────────────────────────────────


def _calc_nli_timeout(source: str, evidence: str) -> int:
    total = len(context_limit(source)) + len(evidence[:500]) + 200
    return min(max(30, int(total * 0.15)), 600)


def _llm_nli_check(evidence: str, source: str) -> str:
    if not evidence or not source:
        return "NEUTRAL"

    prompt = _NLI_VERIFY_PROMPT.format(
        source=context_limit(source), evidence=evidence[:500]
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


# ── Fallback extraction ─────────────────────────────────────────


def _fallback_extract(user_turn: str, thinking: str, text: str,
                      model: str = "day_extract",
                      pulse_context: Optional[str] = None) -> Optional[Dict[str, Any]]:
    from extract_llm import SYSTEM_FALLBACK, _parse_json, _call_with_8082_retry

    parts = [
        "=== user_turn ===", user_turn or "(empty)",
        "", "=== thinking ===", thinking or "(empty)",
        "", "=== text ===", text or "(empty)",
    ]
    source_text = "\n".join(parts)

    system = SYSTEM_FALLBACK
    if pulse_context:
        system = f"{pulse_context}\n\n{system}"

    try:
        meta = _call_with_8082_retry(
            call_llm,
            [{"role": "system", "content": system},
             {"role": "user", "content": source_text}],
            model=model,
            max_tokens=1024, temperature=0.1, timeout=300,
            json_mode=True, return_meta=True,
        )
    except Exception:
        return None

    raw = meta["content"]
    parsed = _parse_json(raw, "fallback")
    if parsed is None:
        return None

    ex = parsed.get("extractions", [])
    if not isinstance(ex, list):
        return None

    rubric = parsed.get("rubric_evaluation", {})
    if rubric:
        print(f"  [fallback] rubric: C={rubric.get('caution','?')} "
              f"F={rubric.get('faithfulness','?')} U={rubric.get('usefulness','?')}",
              flush=True)

    for e in ex:
        if "fact_type" not in e:
            e["fact_type"] = "text"

    return {"extractions": ex, "usage": meta["usage"],
            "timings": meta["timings"], "elapsed_ms": meta["elapsed_ms"]}
