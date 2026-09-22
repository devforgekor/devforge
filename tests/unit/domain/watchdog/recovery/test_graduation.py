#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/recovery/
"""Tests for RecoveryCoordinator (B2)."""
from __future__ import annotations

from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator


def test_plan_returns_action() -> None:
    reg = TrackerRegistry()
    c = RecoveryCoordinator(reg)
    a = c.plan("svc:x", "inactive")
    assert a is not None and a.kind == "service"


def test_plan_none_for_alert_only() -> None:
    c = RecoveryCoordinator(TrackerRegistry())
    assert c.plan("syssvc:caddy", "down") is None


def test_plan_none_when_circuit_open() -> None:
    reg = TrackerRegistry()
    t = reg.get("svc:x")
    for _ in range(3):
        t.record_failure()
    assert RecoveryCoordinator(reg).plan("svc:x", "down") is None


def test_plan_includes_backoff() -> None:
    reg = TrackerRegistry()
    reg.get("svc:x").record_failure()          # consecutive=1 → 10s ±10%
    a = RecoveryCoordinator(reg).plan("svc:x", "inactive")
    assert a is not None and 9 <= a.backoff_sec <= 11


def test_record_success_resets() -> None:
    reg = TrackerRegistry()
    reg.get("svc:x").record_failure()
    RecoveryCoordinator(reg).record_result("svc:x", True)
    assert reg.get("svc:x").state.value == "HEALTHY"


def test_record_failure_advances() -> None:
    reg = TrackerRegistry()
    c = RecoveryCoordinator(reg)
    c.record_result("svc:x", False)
    assert reg.get("svc:x").state.value == "DEGRADED"


def test_should_escalate_on_down() -> None:
    reg = TrackerRegistry()
    c = RecoveryCoordinator(reg)
    for _ in range(5):
        c.record_result("svc:x", False)
    assert c.should_escalate("svc:x") is True


def test_custom_strategy_injection() -> None:
    class Stub:
        def kind_for(self, component: str) -> str:
            return "service"

        def create_action(self, component: str, state: str, reason: str, backoff_sec: int):  # type: ignore[no-untyped-def]
            from devforge.ports.types import RecoveryAction
            return RecoveryAction(component, "stub", reason, 0)

    a = RecoveryCoordinator(TrackerRegistry(), strategy=Stub()).plan("svc:x", "r")
    assert a is not None and a.kind == "stub"
