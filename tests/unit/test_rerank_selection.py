#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_rerank_selection.py
"""OpenRouter rerank role: free-only selection + family scoring."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "proxies"))

import refresh_openrouter_free_models as rr  # noqa: E402


def test_rerank_family_should_rank_cohere_four_above_nvidia():
    assert rr._rerank_family_score("cohere/rerank-4-pro") > rr._rerank_family_score(
        "nvidia/llama-nemotron-rerank-vl-1b-v2:free"
    )
    assert rr._rerank_family_score("unrelated/model") == 0.0


def test_free_filter_should_exclude_paid_models(monkeypatch):
    catalog_raw = [
        {"id": "cohere/rerank-4-pro"},
        {"id": "nvidia/llama-nemotron-rerank-vl-1b-v2:free"},
    ]
    monkeypatch.setattr(rr, "fetch_rerank_models", lambda: catalog_raw)
    # capture what _apply_rerank would test (no network): patch retry tester
    tested = []

    def fake_retry(model_id, keys):
        tested.append(model_id)
        return True, "ok"

    monkeypatch.setattr(rr, "_test_rerank_retry", fake_retry)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-dummy")
    monkeypatch.setattr(rr, "RERANK_CONFIG", Path("/tmp/opencode/_rr_test.json"))
    rc = rr._apply_rerank(dry_run=True)
    assert rc == 0
    assert tested == ["nvidia/llama-nemotron-rerank-vl-1b-v2:free"]


def test_model_score_should_support_rerank_role():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "proxies"))
    import model_score

    s = model_score.score({"id": "cohere/rerank-4-fast", "description": "rerank model"}, "rerank")
    assert s > 0
