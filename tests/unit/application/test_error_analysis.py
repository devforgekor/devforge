#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/application/
"""Tests for error-record §2 rules (pure, no I/O)."""
from __future__ import annotations

from devforge.application.error_analysis import build_decision_packet
from devforge.ports.error_analysis import IncidentEvidence


def _inc(
    incident_id: int,
    component: str,
    context: dict | None = None,
    *,
    fail_count: int = 1,
    reopen_count: int = 0,
) -> IncidentEvidence:
    return IncidentEvidence(
        incident_id=incident_id,
        component=component,
        status="open",
        symptom="down",
        fail_count=fail_count,
        reopen_count=reopen_count,
        detected_at="2026-09-26T00:00:00+00:00",
        context=context or {},
    )


def test_should_report_insufficient_evidence_when_no_incidents() -> None:
    packet = build_decision_packet([], since_iso="2026-09-01T00:00:00+00:00")
    assert packet["decision"] == "insufficient_evidence"
    assert packet["root_cause"]["hypothesis"] is None
    assert packet["evidence"] == []


def test_should_propose_when_exit_code_present() -> None:
    packet = build_decision_packet(
        [_inc(1, "svc:devforge-day-cycle", {"exit_code": 15})],
        since_iso="2026-09-01T00:00:00+00:00",
    )
    assert packet["root_cause"]["hypothesis"] == "exit:15"
    assert packet["decision"] == "propose"
    assert packet["severity"] == "low"


def test_should_prefer_exception_signature_over_exit_code() -> None:
    ctx = {"exception": {"type": "TimeoutError"}, "exit_code": 1}
    packet = build_decision_packet([_inc(1, "svc:x", ctx)], since_iso="s")
    assert packet["root_cause"]["hypothesis"] == "exception:TimeoutError"


def test_should_flag_shared_cause_across_components() -> None:
    ctx = {"exception": {"type": "TimeoutError"}}
    packet = build_decision_packet(
        [_inc(1, "svc:a", ctx), _inc(2, "svc:b", ctx)],
        since_iso="s",
    )
    assert packet["cluster"]["shared_cause_confidence"] > 0.5
    assert {e["component"] for e in packet["evidence"]} == {"svc:a", "svc:b"}
    assert all(e["raw_ref"].startswith("incidents:") for e in packet["evidence"])


def test_should_escalate_when_signature_unknown() -> None:
    packet = build_decision_packet([_inc(1, "svc:a", {})], since_iso="s")
    assert packet["root_cause"]["hypothesis"] == "unknown"
    assert packet["decision"] == "escalate"


def test_should_mark_high_severity_on_reopen() -> None:
    packet = build_decision_packet(
        [_inc(1, "svc:a", {"exit_code": 1}, reopen_count=3)],
        since_iso="s",
    )
    assert packet["severity"] == "high"
