#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_rerank_consumer.py
"""Search rerank consumer: ordering, fallback, record preservation."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from lib.research import rerank  # noqa: E402


def test_rerank_records_should_apply_index_order(monkeypatch):
    monkeypatch.setattr(rerank, "rerank_indices", lambda q, d: [2, 0, 1])
    recs = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    out = rerank.rerank_records("q", recs)
    assert [r["title"] for r in out] == ["C", "A", "B"]


def test_rerank_records_should_keep_original_on_failure(monkeypatch):
    monkeypatch.setattr(rerank, "rerank_indices", lambda q, d: None)
    recs = [{"title": "A"}, {"title": "B"}]
    assert rerank.rerank_records("q", recs) == recs


def test_rerank_records_should_preserve_omitted_items(monkeypatch):
    monkeypatch.setattr(rerank, "rerank_indices", lambda q, d: [1])  # only one index
    recs = [{"title": "A"}, {"title": "B"}, {"title": "C"}]
    out = rerank.rerank_records("q", recs)
    assert [r["title"] for r in out] == ["B", "A", "C"]  # omitted appended, order stable


def test_rerank_indices_should_skip_single_doc(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(rerank, "_rerank_openrouter", lambda *a: called.__setitem__("n", 1))
    assert rerank.rerank_indices("q", ["only"]) is None
    assert called["n"] == 0


def test_rerank_should_prefer_openrouter_then_local(monkeypatch):
    monkeypatch.setattr(rerank, "_rerank_openrouter", lambda q, d, c: [1, 0])
    monkeypatch.setattr(rerank, "_rerank_local", lambda q, d: [0, 1])
    assert rerank.rerank_indices("q", ["a", "b"]) == [1, 0]

    monkeypatch.setattr(rerank, "_rerank_openrouter", lambda q, d, c: None)
    assert rerank.rerank_indices("q", ["a", "b"]) == [0, 1]


def test_openrouter_chain_should_only_use_free(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-dummy")
    captured = []

    def fake_call(url, body, headers, timeout):
        captured.append(body["model"])
        return [{"index": 0}]

    monkeypatch.setattr(rerank, "_call_rerank", fake_call)
    cfg = {"primary": "cohere/rerank-4-pro", "chain": ["nvidia/x:free"]}
    rerank._rerank_openrouter("q", ["a", "b"], cfg)
    assert captured == ["nvidia/x:free"]  # paid primary filtered out
