#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/llm/
"""Tests for the §2 reasoning hypothesis adapter (pure parsing helpers)."""
from __future__ import annotations

from devforge.adapters.driven.llm.reasoning_hypothesis import _model_chain, _parse_hypotheses


def test_parse_hypotheses_tolerates_fences_and_clamps_confidence() -> None:
    text = '```json\n[{"hypothesis":"x","confidence":1.5,"rationale":"r"}]\n```'
    parsed = _parse_hypotheses(text)
    assert len(parsed) == 1
    assert parsed[0].hypothesis == "x"
    assert parsed[0].confidence == 1.0


def test_parse_hypotheses_returns_empty_on_garbage() -> None:
    assert _parse_hypotheses("no json here") == []
    assert _parse_hypotheses("") == []
    assert _parse_hypotheses("[not-an-object]") == []


def test_parse_hypotheses_skips_entries_without_hypothesis() -> None:
    assert _parse_hypotheses('[{"confidence": 0.9, "rationale": "r"}]') == []


def test_model_chain_strips_prefix_and_dedupes() -> None:
    chain = _model_chain(
        {"primary": "openrouter/a/x:free", "chain": ["openrouter/a/x:free", "openrouter/b/y:free"]}
    )
    assert chain == ["a/x:free", "b/y:free"]
