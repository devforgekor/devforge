#!/usr/bin/env python3
# Status: experimental
# Path: scripts/proxies/refresh_openrouter_free_models.py, error analysis model selection
"""Role-aware OpenRouter model scoring + Artificial Analysis enrichment.

Scores a model catalog entry for a given role. Extracted from
refresh_openrouter_free_models.py so both coding and reasoning selection share
one implementation (AGENTS.md 5 — extend, don't duplicate).

- coding:    Artificial Analysis coding_index (+ context bonus), heuristic fallback
- reasoning: Artificial Analysis intelligence_index (+ context bonus), heuristic fallback

Layered benchmark source:
  1. OpenRouter catalog `benchmarks.artificial_analysis` (inline, may be absent)
  2. Artificial Analysis free API (enrich missing indices; key from KV)

Heuristic fallback is hard-capped below any benchmarked model so models with a
real index always outrank guesswork.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Role → primary benchmark key plus fallback composition weights.
# [WHY] Not every free model has its primary index (AA evaluates only a subset).
# A secondary index (same AA family) gives a defensible partial score instead of
# collapsing every unknown model to the same heuristic cap.
#   coding:    primary coding_index    → fallback agentic*0.6 + intelligence*0.4
#   reasoning: primary intelligence    → fallback coding*0.5 + agentic*0.5
ROLE_BENCHMARK: dict[str, str] = {
    "coding": "coding_index",
    "reasoning": "intelligence_index",
    "rerank": "",
}
ROLE_FALLBACK_WEIGHTS: dict[str, tuple[tuple[str, float], ...]] = {
    "coding": (("agentic_index", 0.6), ("intelligence_index", 0.4)),
    "reasoning": (("coding_index", 0.5), ("agentic_index", 0.5)),
    "rerank": (),
}

# Keywords that hint at capability when no benchmark index is available.
ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "coding": ("code", "coding", "agentic"),
    "reasoning": ("reasoning", "think", "r1", "qwen", "deepseek", "reason"),
    # [WHY] 리랭커는 AA 지수가 없다. 이름의 계열(rerank/bge/cohere/jina/voyage/nemotron)
    # 과 정렬 품질 관행(cohere/voyage > qwen > nvidia)으로 점수를 매긴다.
    "rerank": ("rerank", "cohere", "voyage", "jina", "bge", "nemotron", "qwen"),
}

# No-benchmark scores are capped below any real index (free models run ~30-60).
_HEURISTIC_CAP = 29.9
_HEURISTIC_BASE = 25.0
_KEYWORD_BONUS = 10.0
_AGENTIC_BONUS = 8.0
_CONTEXT_BONUS = 5.0
_CONTEXT_BONUS_MIN = 100_000
_CONTEXT_BONUS_MIN_HIGH = 256_000

# ── Artificial Analysis free API (enrichment source) ──
AA_API_URL = "https://artificialanalysis.ai/api/v2/language/models/free"
AA_API_KEY_ENV = "ARTIFICIALANALYSIS_API_KEY"
AA_CACHE_FILE = Path.home() / ".cache/devforge/artificial_analysis.json"
AA_CACHE_TTL_SEC = 24 * 60 * 60


def _context_bonus(model: dict[str, Any]) -> float:
    context = model.get("context_length") or 0
    if context >= _CONTEXT_BONUS_MIN_HIGH:
        return _CONTEXT_BONUS
    return 0.0


def _benchmark_value(model: dict[str, Any], key: str) -> float | None:
    benchmarks = model.get("benchmarks", {}) or {}
    aa = benchmarks.get("artificial_analysis", {}) or {}
    value = aa.get(key)
    return float(value) if value is not None else None


def _heuristic_score(model: dict[str, Any], role: str) -> float:
    desc = (model.get("description") or "").lower()
    keywords = ROLE_KEYWORDS.get(role, ())
    bonus = 0.0
    for kw in keywords:
        if kw in desc:
            bonus += _KEYWORD_BONUS
            break
    if role == "coding" and "agentic" in desc:
        bonus += _AGENTIC_BONUS
    if (model.get("context_length") or 0) >= _CONTEXT_BONUS_MIN:
        bonus += _CONTEXT_BONUS
    return min(_HEURISTIC_BASE + bonus, _HEURISTIC_CAP)


def _composite_fallback(model: dict[str, Any], role: str) -> float | None:
    """Weighted score from secondary indices when the primary is absent.

    Returns None if no weighted input is available. A small penalty (×0.9) keeps
    composite scores below a genuine primary index so primary-bearing models
    always rank higher.
    """
    total = 0.0
    weight_sum = 0.0
    for key, w in ROLE_FALLBACK_WEIGHTS.get(role, ()):
        value = _benchmark_value(model, key)
        if value is not None:
            total += value * w
            weight_sum += w
    if weight_sum == 0.0:
        return None
    return (total / weight_sum) * 0.9


def score(model: dict[str, Any], role: str = "coding") -> float:
    """Score a model for the given role (higher is better)."""
    if role not in ROLE_BENCHMARK:
        raise ValueError(f"unknown role: {role!r} (expected one of {sorted(ROLE_BENCHMARK)})")
    if not ROLE_BENCHMARK[role]:  # rerank: no AA index — heuristic only
        return _heuristic_score(model, role)
    value = _benchmark_value(model, ROLE_BENCHMARK[role])
    if value is not None:
        return value + _context_bonus(model)
    composite = _composite_fallback(model, role)
    if composite is not None:
        return composite
    return _heuristic_score(model, role)


def rank(models: list[dict[str, Any]], role: str = "coding") -> list[dict[str, Any]]:
    """Return models annotated with `_score`, sorted high→low (stable)."""
    for m in models:
        m["_score"] = score(m, role)
    return sorted(models, key=lambda m: m["_score"], reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# Artificial Analysis enrichment
# ─────────────────────────────────────────────────────────────────────────────


def aa_normalize_slug(model_id: str) -> str:
    """OpenRouter model id → AA slug convention.

    org stripped, ':free' dropped, '.'→'-', and trailing date suffix removed
    (OpenRouter canonical_slug appends e.g. '-20260814'; AA omits it).
    """
    base = model_id.split(":")[0].split("/")[-1]
    base = base.lower().replace(".", "-")
    base = re.sub(r"-\d{8}$", "", base)   # drop trailing YYYYMMDD
    return base


def aa_lookup_slug(model: dict[str, Any]) -> str:
    """Best AA lookup key for an OpenRouter model entry.

    Prefers `id` (no date suffix) over `canonical_slug` (which appends a date
    suffix that AA omits). Falls back to canonical_slug when id is absent.
    """
    source = str(model.get("id") or model.get("canonical_slug") or "")
    return aa_normalize_slug(source)


def fetch_aa_models(api_key: str | None = None, timeout: int = 30) -> dict[str, dict[str, Any]]:
    """Fetch AA free-tier language models as {slug: entry}.

    Returns {} when no key is configured or the fetch fails (best-effort:
    enrichment must never break model selection).
    """
    key = api_key or os.environ.get(AA_API_KEY_ENV, "")
    if not key:
        return {}
    try:
        out: dict[str, dict[str, Any]] = {}
        page = 1
        while True:
            req = urllib.request.Request(
                f"{AA_API_URL}?page={page}",
                headers={"x-api-key": key, "User-Agent": "devforge-aa"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode())
            for m in body.get("data", []):
                if m.get("slug"):
                    out[m["slug"]] = m
            pag = body.get("pagination", {})
            if not pag.get("has_more") or page >= pag.get("total_pages", 1):
                break
            page += 1
        return out
    except (urllib.error.URLError, ValueError, OSError):
        return {}


def enrich_with_aa(
    models: list[dict[str, Any]],
    aa_models: dict[str, dict[str, Any]],
) -> int:
    """Fill missing `benchmarks.artificial_analysis` indices from AA data.

    Matches by AA slug convention. Only fills keys the model lacks; never
    overwrites OpenRouter-provided values. Returns count of enriched models.
    """
    if not aa_models:
        return 0
    n = 0
    for m in models:
        entry = aa_models.get(aa_lookup_slug(m))
        if not entry:
            continue
        ev = entry.get("evaluations", {}) or {}
        aa = m.setdefault("benchmarks", {}).setdefault("artificial_analysis", {})
        changed = False
        for aa_key, ev_key in (
            ("intelligence_index", "artificial_analysis_intelligence_index"),
            ("coding_index", "artificial_analysis_coding_index"),
            ("agentic_index", "artificial_analysis_agentic_index"),
        ):
            if aa.get(aa_key) is None and ev.get(ev_key) is not None:
                aa[aa_key] = ev[ev_key]
                changed = True
        if changed:
            n += 1
    return n


def load_aa_cache() -> dict[str, dict[str, Any]]:
    if not AA_CACHE_FILE.exists():
        return {}
    try:
        entry = json.loads(AA_CACHE_FILE.read_text())
        import time
        if time.time() - entry.get("ts", 0) < AA_CACHE_TTL_SEC:
            models = entry.get("models", {})
            return models if isinstance(models, dict) else {}
    except (ValueError, OSError):
        pass
    return {}


def save_aa_cache(aa_models: dict[str, dict[str, Any]]) -> None:
    import time
    AA_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    AA_CACHE_FILE.write_text(json.dumps({"ts": time.time(), "models": aa_models}))
