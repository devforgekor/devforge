#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for RecoveryCoordinator."""
from __future__ import annotations

from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.types import HealthCheckResult


class TestRecoveryCoordinator:
    def _make_coordinator(self, failure_threshold: int = 3) -> RecoveryCoordinator:
        tracker = ComponentTracker(failure_threshold=failure_threshold)
        return RecoveryCoordinator(tracker=tracker)

    def test_plan_recovery_returns_action(self) -> None:
        coord = self._make_coordinator()
        # Simulate 2 failures
        for _ in range(2):
            coord._tracker.record_check(
                HealthCheckResult(component="c", ok=False, detail="fail")
            )
        action = coord.plan_recovery("c")
        assert action is not None
        assert action.component == "c"

    def test_should_not_escalate_when_healthy(self) -> None:
        coord = self._make_coordinator()
        coord._tracker.record_check(
            HealthCheckResult(component="c", ok=True, detail="ok")
        )
        assert coord.should_escalate("c") is False
