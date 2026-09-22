#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/monitoring/
"""Tests for ComponentTracker + TrackerRegistry (A3)."""
from __future__ import annotations

import time

from devforge.domain.watchdog.monitoring.tracker import (
    ComponentTracker,
    TrackerRegistry,
)
from devforge.ports.types import ComponentState, HealthCheck


def test_initial_state_healthy() -> None:
    t = ComponentTracker("svc:x")
    assert t.state == ComponentState.HEALTHY and t.consecutive_fail == 0


def test_one_failure_degraded() -> None:
    t = ComponentTracker("svc:x")
    assert t.record_failure() is True
    assert t.state == ComponentState.DEGRADED and t.consecutive_fail == 1


def test_three_failures_unhealthy() -> None:
    t = ComponentTracker("svc:x")
    for _ in range(3):
        t.record_failure()
    assert t.state == ComponentState.UNHEALTHY and t.consecutive_fail == 3


def test_five_failures_down() -> None:
    t = ComponentTracker("svc:x")
    for _ in range(5):
        t.record_failure()
    assert t.state == ComponentState.DOWN


def test_circuit_opens_at_three() -> None:
    t = ComponentTracker("svc:x")
    for _ in range(3):
        t.record_failure()
    assert t.circuit_open_until > 0 and t.can_retry() is False


def test_circuit_half_open_after_timeout() -> None:
    t = ComponentTracker("svc:x")
    for _ in range(3):
        t.record_failure()
    t.circuit_open_until = time.monotonic() - 1
    assert t.can_retry() is True


def test_success_resets_consecutive_not_total() -> None:
    t = ComponentTracker("svc:x")
    t.record_failure()
    t.record_failure()
    t.record_success()
    assert t.consecutive_fail == 0 and t.fail_count == 2
    assert t.state == ComponentState.HEALTHY


def test_alert_dedup() -> None:
    t = ComponentTracker("svc:x")
    assert t.can_alert(dedup_sec=10) is True
    assert t.can_alert(dedup_sec=10) is False
    t.last_alert_ts = time.monotonic() - 11
    assert t.can_alert(dedup_sec=10) is True


def test_backoff_grows() -> None:
    t = ComponentTracker("svc:x")
    assert t.backoff_sec() == 0
    t.record_failure()
    assert 9 <= t.backoff_sec() <= 11


def test_backoff_reset_after_10min() -> None:
    t = ComponentTracker("svc:x")
    t.record_failure()
    t.record_failure()
    t.last_fail_ts = time.monotonic() - 601
    t.record_success()
    assert t.fail_count == 0


def test_record_check_bridge_success() -> None:
    t = ComponentTracker("svc:x")
    assert t.record_check(HealthCheck(component="svc:x", is_healthy=True, detail="ok")) is False
    assert t.state == ComponentState.HEALTHY


def test_record_check_bridge_failure() -> None:
    t = ComponentTracker("svc:x")
    assert t.record_check(HealthCheck(component="svc:x", is_healthy=False, detail="down")) is True
    assert t.state == ComponentState.DEGRADED


def test_serialization_roundtrip() -> None:
    t = ComponentTracker("svc:x")
    t.record_failure()
    t.record_failure()
    t.can_alert()
    r = ComponentTracker.from_dict(t.to_dict())
    assert r.name == "svc:x" and r.state == ComponentState.DEGRADED
    assert r.consecutive_fail == 2 and r.last_alert_ts > 0


def test_registry_get_creates_and_restores() -> None:
    reg = TrackerRegistry()
    assert reg.get("svc:a").name == "svc:a"
    assert set(reg.all()) == {"svc:a"}
    t = ComponentTracker("svc:b")
    t.record_failure()
    reg2 = TrackerRegistry()
    reg2.restore({"svc:b": t.to_dict()})
    assert reg2.get("svc:b").consecutive_fail == 1


def test_recovery_attempt_gate_non_blocking() -> None:
    t = ComponentTracker("svc:x")
    assert t.can_attempt_recovery() is True
    t.schedule_next_attempt(10)
    assert t.can_attempt_recovery() is False
    t.next_attempt_at = time.monotonic() - 1
    assert t.can_attempt_recovery() is True


def test_next_attempt_at_persisted() -> None:
    t = ComponentTracker("svc:x")
    t.schedule_next_attempt(42)
    r = ComponentTracker.from_dict(t.to_dict())
    assert r.next_attempt_at == t.next_attempt_at
