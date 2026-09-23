#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Tests for role-aware OpenRouter model scoring (model_score.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "proxies"))

import model_score  # noqa: E402


def _model(mid: str, *, coding=None, intelligence=None, agentic=None, context=None, desc="") -> dict:
    aa: dict = {}
    if coding is not None:
        aa["coding_index"] = coding
    if intelligence is not None:
        aa["intelligence_index"] = intelligence
    if agentic is not None:
        aa["agentic_index"] = agentic
    m: dict = {"id": mid, "description": desc}
    if aa:
        m["benchmarks"] = {"artificial_analysis": aa}
    if context is not None:
        m["context_length"] = context
    return m


class TestBenchmarkScore:
    def test_should_use_coding_index_for_coding_role(self) -> None:
        m = _model("a/x:free", coding=45.0)
        assert model_score.score(m, "coding") == 45.0

    def test_should_use_intelligence_index_for_reasoning_role(self) -> None:
        m = _model("a/x:free", intelligence=55.0)
        assert model_score.score(m, "reasoning") == 55.0

    def test_should_add_context_bonus_when_context_large(self) -> None:
        m = _model("a/x:free", coding=40.0, context=300_000)
        assert model_score.score(m, "coding") == 45.0

    def test_should_not_add_bonus_below_threshold(self) -> None:
        m = _model("a/x:free", coding=40.0, context=50_000)
        assert model_score.score(m, "coding") == 40.0


class TestRoleIsolation:
    def test_should_score_roles_differently_when_indices_differ(self) -> None:
        m = _model("a/x:free", coding=80.0, intelligence=30.0)
        assert model_score.score(m, "coding") > model_score.score(m, "reasoning")

    def test_should_use_composite_fallback_when_primary_missing(self) -> None:
        # coding primary absent, but intelligence+agentic present → composite used
        m = _model("a/x:free", intelligence=40.0)
        s = model_score.score(m, "coding")
        assert s > 30.0  # composite, not heuristic cap
        assert s < 40.0  # composite (×0.9) stays below a real primary index


class TestCompositeFallback:
    def test_should_penalize_composite_below_primary(self) -> None:
        primary = _model("a/p:free", coding=50.0)
        composite = _model("a/c:free", agentic=50.0)
        assert model_score.score(primary, "coding") > model_score.score(composite, "coding")

    def test_should_return_heuristic_when_no_indices_at_all(self) -> None:
        m = _model("a/x:free", desc="code agentic")
        assert model_score.score(m, "coding") < 30.0

    def test_should_use_coding_and_agentic_for_reasoning_fallback(self) -> None:
        m = _model("a/x:free", coding=60.0, intelligence=None)
        # reasoning composite = (coding*0.5 + agentic*0.5)/1 * 0.9 with only coding
        assert 40.0 < model_score.score(m, "reasoning") < 60.0


class TestHeuristicFallback:
    def test_should_cap_heuristic_below_any_benchmarked_model(self) -> None:
        heuristic = _model("a/x:free", desc="coding agentic")
        assert model_score.score(heuristic, "coding") < 30.0

    def test_should_rank_benchmarked_above_unbenchmarked(self) -> None:
        bench = _model("a/bench:free", coding=31.0)
        heur = _model("a/heur:free", desc="code agentic " + "x" * 10)
        assert model_score.score(bench, "coding") > model_score.score(heur, "coding")

    def test_should_bonus_reasoning_keyword(self) -> None:
        with_kw = _model("a/k:free", desc="a reasoning model")
        without = _model("a/n:free", desc="generic model")
        assert model_score.score(with_kw, "reasoning") > model_score.score(without, "reasoning")

    def test_should_not_apply_coding_agentic_bonus_to_reasoning(self) -> None:
        # 'agentic' is coding-specific; reasoning heuristic must not use it
        agentic = _model("a/x:free", desc="agentic agent")
        base = _model("a/y:free", desc="plain")
        assert model_score.score(agentic, "reasoning") == model_score.score(base, "reasoning")


class TestAAMatching:
    def test_should_normalize_openrouter_id_to_aa_slug(self) -> None:
        assert model_score.aa_normalize_slug("qwen/qwen3.8-27b:free") == "qwen3-8-27b"
        assert model_score.aa_normalize_slug("z-ai/glm-5.2:free") == "glm-5-2"

    def test_should_strip_trailing_date_suffix(self) -> None:
        assert model_score.aa_normalize_slug("x/y-20260814") == "y"

    def test_should_prefer_id_over_canonical_slug(self) -> None:
        m = {"id": "a/foo-1.2:free", "canonical_slug": "a/foo-1.2-20260101"}
        assert model_score.aa_lookup_slug(m) == "foo-1-2"

    def test_should_enrich_missing_indices_only(self) -> None:
        models = [{"id": "a/foo:free",
                   "benchmarks": {"artificial_analysis": {"coding_index": 50.0}}}]
        aa = {"foo": {"evaluations": {
            "artificial_analysis_intelligence_index": 30.0,
            "artificial_analysis_coding_index": 99.0,  # must NOT overwrite
        }}}
        n = model_score.enrich_with_aa(models, aa)
        aa_block = models[0]["benchmarks"]["artificial_analysis"]
        assert n == 1
        assert aa_block["intelligence_index"] == 30.0
        assert aa_block["coding_index"] == 50.0  # preserved

    def test_should_noop_when_aa_empty(self) -> None:
        models = [{"id": "a/foo:free"}]
        assert model_score.enrich_with_aa(models, {}) == 0


class TestRank:
    def test_should_annotate_and_sort_descending(self) -> None:
        models = [
            _model("a/low:free", coding=10.0),
            _model("a/high:free", coding=90.0),
            _model("a/mid:free", coding=50.0),
        ]
        ranked = model_score.rank(models, "coding")
        assert [m["id"] for m in ranked] == ["a/high:free", "a/mid:free", "a/low:free"]
        assert all("_score" in m for m in ranked)

    def test_should_rank_by_role(self) -> None:
        models = [
            _model("a/coder:free", coding=90.0, intelligence=10.0),
            _model("a/thinker:free", coding=10.0, intelligence=90.0),
        ]
        assert model_score.rank(models, "reasoning")[0]["id"] == "a/thinker:free"
        # re-run for coding (rank mutates _score in place)
        assert model_score.rank(models, "coding")[0]["id"] == "a/coder:free"


class TestValidation:
    def test_should_raise_on_unknown_role(self) -> None:
        with pytest.raises(ValueError):
            model_score.score(_model("a/x:free", coding=1.0), "poetry")
