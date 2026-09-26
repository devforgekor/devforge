#!/usr/bin/env python3
# Status: experimental
# Path: tests/fitness/
"""Fitness: watchdog incident contract invariants.

Freezes the two contract decisions the incident pipeline depends on:
  1. Record policy (wd-q2-incident-record-policy) — only systemd-family outcomes
     write `watchdog_incidents` rows; memory/llm/disk/heartbeat do not.
  2. context_jsonb SSOT (error-record design §1) — structured diagnostics live in
     `context_jsonb`/`action_error`; deprecated text `context` is retained only
     until its readers are gone.
Divergence here silently corrupts incident history, so it is CI-enforced.
"""
from __future__ import annotations

from pathlib import Path

from devforge.application.watchdog_service import INCIDENT_COMPONENT_PREFIXES, writes_incident
from devforge.domain import models
from devforge.domain.watchdog.recovery.strategies import classify_recovery_kind

ROOT = Path(__file__).resolve().parents[2]

_ALERT_ONLY = (
    "system:memory",
    "system:disk:/",
    "llm:day-extract",
    "heartbeat:day_enrich",
    "pipeline:embed",
    "infra:inference",
    "dataimpulse:toki31",
)


def test_record_policy_prefixes_frozen() -> None:
    assert INCIDENT_COMPONENT_PREFIXES == ("svc:", "timer:", "oneshot:", "syssvc:")


def test_writes_incident_policy() -> None:
    assert writes_incident("svc:devforge-day-cycle")
    assert writes_incident("timer:devforge-dev-poll.timer")
    assert writes_incident("oneshot:devforge-backup.service")
    assert writes_incident("syssvc:caddy")
    for alert_only in _ALERT_ONLY:
        assert not writes_incident(alert_only), alert_only


def test_recovery_routing_covers_families() -> None:
    assert classify_recovery_kind("svc:devforge-day-cycle") == "service"
    assert classify_recovery_kind("svc:container-devforge-worker") == "container"
    assert classify_recovery_kind("svc:container-postgres") is None  # alert-only exact
    assert classify_recovery_kind("timer:devforge-dev-poll.timer") == "timer_kick"
    assert classify_recovery_kind("oneshot:devforge-backup.service") == "oneshot"
    assert classify_recovery_kind("llm:day-extract") == "cascade"
    assert classify_recovery_kind("system:memory") == "oom"
    assert classify_recovery_kind("system:disk:/") is None  # alert-only
    assert classify_recovery_kind("heartbeat:day_enrich") is None  # no recovery


def test_incident_context_jsonb_ssot_columns() -> None:
    cols = models.WatchdogIncident.__table__.columns
    assert "context_jsonb" in cols and not cols["context_jsonb"].nullable
    assert "action_error" in cols
    # deprecated text consumers still exist; column is intentionally retained (§3.2)
    assert "context" in cols


def test_incident_repository_writes_structured_context() -> None:
    text = (ROOT / "src/devforge/adapters/driven/storage/incident_pg.py").read_text(encoding="utf-8")
    assert "context_jsonb" in text, "incident_pg must write context_jsonb"
    assert "action_error" in text, "incident_pg must write action_error"
