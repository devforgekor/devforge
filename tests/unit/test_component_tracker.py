#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for ComponentTracker state machine."""
from __future__ import annotations

from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.ports.types import ComponentState, HealthCheckResult


class TestComponentTracker:
    def test_unknown_component_returns_none(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        assert tracker.get_status("nonexistent") is None

    def test_first_success(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        result = HealthCheckResult(component="c", ok=True, detail="ok")
        tracker.record_check(result)
        status = tracker.get_status("c")
        assert status is not None
        assert status.state == ComponentState.HEALTHY
        assert status.consecutive_failures == 0

    def test_first_failure(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        result = HealthCheckResult(component="c", ok=False, detail="fail")
        tracker.record_check(result)
        status = tracker.get_status("c")
        assert status is not None
        assert status.state == ComponentState.DEGRADED
        assert status.consecutive_failures == 1

    def test_degraded_after_2_failures(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        for _ in range(2):
            tracker.record_check(HealthCheckResult(component="c", ok=False, detail="fail"))
        status = tracker.get_status("c")
        assert status is not None
        assert status.state == ComponentState.DEGRADED

    def test_critical_after_threshold_failures(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        for _ in range(3):
            tracker.record_check(HealthCheckResult(component="c", ok=False, detail="fail"))
        status = tracker.get_status("c")
        assert status is not None
        assert status.state == ComponentState.CRITICAL

    def test_success_resets_failures(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        for _ in range(2):
            tracker.record_check(HealthCheckResult(component="c", ok=False, detail="fail"))
        tracker.record_check(HealthCheckResult(component="c", ok=True, detail="ok"))
        status = tracker.get_status("c")
        assert status is not None
        assert status.consecutive_failures == 0

    def test_all_statuses(self) -> None:
        tracker = ComponentTracker(failure_threshold=3)
        tracker.record_check(HealthCheckResult(component="a", ok=True, detail="ok"))
        tracker.record_check(HealthCheckResult(component="b", ok=False, detail="fail"))
        statuses = tracker.all_statuses()
        assert len(statuses) == 2
        names = {s.name for s in statuses}
        assert names == {"a", "b"}
