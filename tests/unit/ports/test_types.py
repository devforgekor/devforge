#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/ports/
"""Tests for watchdog value objects (ports/types.py)."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from devforge.ports.types import ComponentState, HealthCheck, Incident, RecoveryAction


def test_component_state_matches_legacy() -> None:
    assert [s.value for s in ComponentState] == ["HEALTHY", "DEGRADED", "UNHEALTHY", "DOWN"]


def test_health_check_is_frozen() -> None:
    hc = HealthCheck(component="svc:x", is_healthy=True, detail="OK")
    with pytest.raises(FrozenInstanceError):
        hc.is_healthy = False  # type: ignore[misc]


def test_health_check_optional_metrics_default_none() -> None:
    hc = HealthCheck(component="svc:x", is_healthy=False, detail="down")
    assert hc.metric_value is None and hc.threshold is None


def test_recovery_action_default_backoff() -> None:
    a = RecoveryAction(component="svc:x", kind="service", reason="down")
    assert a.backoff_sec == 0


def test_incident_defaults() -> None:
    now = datetime.now(timezone.utc)
    i = Incident(id=None, dedup_key="svc:x:down", component="svc:x", status="open",
                 symptom="down", context=None, detected_at=now, last_seen_at=now)
    assert i.fail_count == 1 and i.reopen_count == 0 and i.resolved_at is None
