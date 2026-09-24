#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/core/
"""Tests for OTel/GenAI telemetry + correlation (2026 standard-gap §9)."""
from __future__ import annotations

from devforge.core import telemetry
from devforge.core.telemetry import (
    GEN_AI_USAGE_INPUT_TOKENS,
    GEN_AI_USAGE_OUTPUT_TOKENS,
    correlation,
    current_run_id,
    current_trace_id,
    genai_usage_attributes,
    inject_trace_context,
    new_run_id,
    set_run_id,
    setup_telemetry,
    span,
)


def test_genai_usage_openai_keys() -> None:
    attrs = genai_usage_attributes({"prompt_tokens": 10, "completion_tokens": 20})
    assert attrs == {GEN_AI_USAGE_INPUT_TOKENS: 10, GEN_AI_USAGE_OUTPUT_TOKENS: 20}


def test_genai_usage_llamacpp_keys() -> None:
    attrs = genai_usage_attributes({"tokens_evaluated": 7, "tokens_predicted": 3})
    assert attrs == {GEN_AI_USAGE_INPUT_TOKENS: 7, GEN_AI_USAGE_OUTPUT_TOKENS: 3}


def test_genai_usage_empty_or_partial() -> None:
    assert genai_usage_attributes(None) == {}
    assert genai_usage_attributes({}) == {}
    assert genai_usage_attributes({"prompt_tokens": 5}) == {GEN_AI_USAGE_INPUT_TOKENS: 5}


def test_span_inherits_trace_id_and_new_span_id() -> None:
    assert current_trace_id() == ""
    with span("outer") as outer:
        assert outer.trace_id != ""
        assert current_trace_id() == outer.trace_id
        with span("inner") as inner:
            assert inner.trace_id == outer.trace_id
            assert inner.span_id != outer.span_id
    assert current_trace_id() == ""


def test_span_records_attributes() -> None:
    with span("op", attributes={"a": 1}) as s:
        s.set_attribute("b", "x")
        s.set_attributes({"c": True})
    assert s.attributes == {"a": 1, "b": "x", "c": True}


def test_span_annotates_error_type_on_exception() -> None:
    captured = None
    try:
        with span("bad") as s:
            raise ValueError("boom")
    except ValueError:
        captured = s
    assert captured is not None
    assert captured.attributes["error.type"] == "ValueError"


def test_correlation_combines_trace_and_run() -> None:
    set_run_id("run-abc")
    assert current_run_id() == "run-abc"
    assert correlation() == {"run_id": "run-abc"}
    with span("op") as s:
        assert correlation() == {"trace_id": s.trace_id, "run_id": "run-abc"}
    set_run_id(None)


def test_new_run_id_has_prefix() -> None:
    rid = new_run_id("watchdog")
    assert rid.startswith("watchdog-")
    assert len(rid) > len("watchdog-")


def test_inject_trace_context_processor() -> None:
    event: dict = {}
    set_run_id("r1")
    assert inject_trace_context(None, "info", event) == {"run_id": "r1"}
    with span("op"):
        event2 = inject_trace_context(None, "info", {})
        assert set(event2) == {"trace_id", "run_id"}
    set_run_id(None)


def test_setup_telemetry_disabled_without_backend(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_TRACES_EXPORTER", raising=False)
    monkeypatch.setattr(telemetry, "_setup_done", False)
    assert setup_telemetry("test") is False
