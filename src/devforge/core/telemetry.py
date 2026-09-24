#!/usr/bin/env python3
# Status: experimental
# Path: core/logging.py, adapters/driven/llm/local_adapter.py, adapters/driven/storage/incident_pg.py, mcp/server.py
"""Lightweight OpenTelemetry tracing + GenAI semantic conventions (2026 standard-gap §9).

Design (per plan §9/§16): instrument with the OTel **API** only, so tracing is a
no-op when nothing is configured. The SDK + OTLP exporter live in the optional
`devforge[otel]` extra and are activated solely by `setup_telemetry()` when an
exporter endpoint is present. This keeps the base runtime dependency-free and the
backend a later choice.

`span()` additionally maintains a local trace/run context (contextvars) so
correlation ids exist even without the SDK — `correlation()` is embedded into
incident `context_jsonb` and MCP audit `observations`.
"""

from __future__ import annotations

import contextlib
import logging
import os
from contextvars import ContextVar
from typing import Any, Iterator, Optional
from uuid import uuid4

log = logging.getLogger(__name__)

try:  # OTel API — base dependency; absent only in stripped test envs.
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind as _SpanKind

    _OTEL = True
except Exception:  # noqa: BLE001 — instrumentation must never break the app
    _otel_trace = None
    _SpanKind = None
    _OTEL = False

# ── GenAI semantic conventions (semantic-conventions-genai) ──
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_PROVIDER_NAME = "gen_ai.provider.name"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_CALL_ID = "gen_ai.tool.call.id"
ERROR_TYPE = "error.type"

# Public alias so callers can request a GenAI CLIENT span (None-safe without OTel).
SpanKind = _SpanKind

# Legacy GenAI keys still returned by llama.cpp's OpenAI-compatible endpoint.
_INPUT_TOKEN_KEYS = ("prompt_tokens", "input_tokens", "tokens_evaluated")
_OUTPUT_TOKEN_KEYS = ("completion_tokens", "output_tokens", "tokens_predicted")

_trace_id_var: ContextVar[Optional[str]] = ContextVar("devforge_trace_id", default=None)
_span_id_var: ContextVar[Optional[str]] = ContextVar("devforge_span_id", default=None)
_run_id_var: ContextVar[Optional[str]] = ContextVar("devforge_run_id", default=None)
_setup_done = False


def _new_trace_id() -> str:
    return uuid4().hex


def _new_span_id() -> str:
    return uuid4().hex[:16]


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def set_run_id(run_id: Optional[str]) -> None:
    _run_id_var.set(run_id)


def current_run_id() -> str:
    return _run_id_var.get() or ""


def current_trace_id() -> str:
    """Active trace id (OTel if valid, else local span context); "" outside a span."""
    if _OTEL:
        ctx = _otel_trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    return _trace_id_var.get() or ""


def current_span_id() -> str:
    if _OTEL:
        ctx = _otel_trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.span_id, "016x")
    return _span_id_var.get() or ""


def correlation() -> dict[str, str]:
    """Non-empty trace/run correlation ids for embedding in records."""
    out: dict[str, str] = {}
    tid = current_trace_id()
    rid = current_run_id()
    if tid:
        out["trace_id"] = tid
    if rid:
        out["run_id"] = rid
    return out


def genai_usage_attributes(usage: Optional[dict[str, Any]]) -> dict[str, int]:
    """Map a provider usage dict to GenAI token attributes (pure)."""
    out: dict[str, int] = {}
    if not usage:
        return out
    for key in _INPUT_TOKEN_KEYS:
        if usage.get(key) is not None:
            out[GEN_AI_USAGE_INPUT_TOKENS] = int(usage[key])
            break
    for key in _OUTPUT_TOKEN_KEYS:
        if usage.get(key) is not None:
            out[GEN_AI_USAGE_OUTPUT_TOKENS] = int(usage[key])
            break
    return out


class Span:
    """Thin span wrapper: always records attributes locally, delegates to OTel."""

    def __init__(self, name: str, trace_id: str, span_id: str, attributes: dict[str, Any]) -> None:
        self.name = name
        self.trace_id = trace_id
        self.span_id = span_id
        self.attributes = dict(attributes)
        self._otel: Any = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value
        if self._otel is not None:
            self._otel.set_attribute(key, value)

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        for key, value in attributes.items():
            self.set_attribute(key, value)

    def record_exception(self, exc: BaseException) -> None:
        if self._otel is not None:
            self._otel.record_exception(exc)


@contextlib.contextmanager
def span(
    name: str,
    *,
    kind: Any = None,
    attributes: Optional[dict[str, Any]] = None,
) -> Iterator[Span]:
    """Start a span, propagating trace/run context (works without the SDK)."""
    attrs = dict(attributes or {})
    parent_trace = _trace_id_var.get()
    if _OTEL:
        span_kind = kind if kind is not None else _SpanKind.INTERNAL
        with _otel_trace.get_tracer("devforge").start_as_current_span(
            name, kind=span_kind, attributes=attrs
        ) as otel_span:
            ctx = otel_span.get_span_context()
            trace_id = (
                format(ctx.trace_id, "032x") if ctx.is_valid else (parent_trace or _new_trace_id())
            )
            span_id = format(ctx.span_id, "016x") if ctx.is_valid else _new_span_id()
            wrapper = Span(name, trace_id, span_id, attrs)
            wrapper._otel = otel_span
            t_tok = _trace_id_var.set(trace_id)
            s_tok = _span_id_var.set(span_id)
            try:
                yield wrapper
            except Exception as exc:  # noqa: BLE001 — annotate then re-raise
                wrapper.set_attribute(ERROR_TYPE, type(exc).__name__)
                wrapper.record_exception(exc)
                raise
            finally:
                _trace_id_var.reset(t_tok)
                _span_id_var.reset(s_tok)
    else:
        trace_id = parent_trace or _new_trace_id()
        wrapper = Span(name, trace_id, _new_span_id(), attrs)
        t_tok = _trace_id_var.set(trace_id)
        s_tok = _span_id_var.set(wrapper.span_id)
        try:
            yield wrapper
        except Exception as exc:  # noqa: BLE001
            wrapper.set_attribute(ERROR_TYPE, type(exc).__name__)
            raise
        finally:
            _trace_id_var.reset(t_tok)
            _span_id_var.reset(s_tok)


def setup_telemetry(service_name: str = "devforge") -> bool:
    """Configure the OTel SDK + exporter iff a backend is configured (idempotent)."""
    global _setup_done
    if _setup_done:
        return False
    _setup_done = True
    if not _OTEL:
        log.debug("otel disabled: opentelemetry-api not installed")
        return False
    exporter = os.environ.get("OTEL_TRACES_EXPORTER", "").lower()
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get(
        "OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    if not endpoint and exporter not in ("otlp", "console"):
        log.debug("otel disabled: no OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_TRACES_EXPORTER")
        return False
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
        if exporter == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        else:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        _otel_trace.set_tracer_provider(provider)
        log.info("otel enabled", extra={"endpoint": endpoint or "console"})
        return True
    except Exception as exc:  # noqa: BLE001 — never break startup for telemetry
        log.warning("otel setup failed: %s", exc)
        return False


def inject_trace_context(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: add trace_id/run_id when a context is active."""
    tid = current_trace_id()
    rid = current_run_id()
    if tid:
        event_dict.setdefault("trace_id", tid)
    if rid:
        event_dict.setdefault("run_id", rid)
    return event_dict


__all__ = [
    "Span",
    "SpanKind",
    "correlation",
    "current_run_id",
    "current_span_id",
    "current_trace_id",
    "genai_usage_attributes",
    "inject_trace_context",
    "new_run_id",
    "set_run_id",
    "setup_telemetry",
    "span",
]
