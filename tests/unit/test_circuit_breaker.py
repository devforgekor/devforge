#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for CircuitBreaker."""
from __future__ import annotations

from devforge.domain.watchdog.monitoring.circuit_breaker import CircuitBreaker
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.ports.types import HealthCheckResult


class TestCircuitBreaker:
    def _make_cb(self, failure_threshold: int = 3) -> CircuitBreaker:
        tracker = ComponentTracker(failure_threshold=failure_threshold)
        return CircuitBreaker(tracker=tracker)

    def test_closed_initially(self) -> None:
        cb = self._make_cb()
        assert cb.is_open("c") is False

    def test_opens_after_threshold_failures(self) -> None:
        cb = self._make_cb(failure_threshold=3)
        for _ in range(3):
            cb._tracker.record_check(
                HealthCheckResult(component="c", ok=False, detail="fail")
            )
        assert cb.is_open("c") is True

    def test_success_resets(self) -> None:
        cb = self._make_cb(failure_threshold=3)
        for _ in range(2):
            cb._tracker.record_check(
                HealthCheckResult(component="c", ok=False, detail="fail")
            )
        cb._tracker.record_check(
            HealthCheckResult(component="c", ok=True, detail="ok")
        )
        assert cb.is_open("c") is False

    def test_separate_components(self) -> None:
        cb = self._make_cb(failure_threshold=3)
        for _ in range(3):
            cb._tracker.record_check(
                HealthCheckResult(component="a", ok=False, detail="fail")
            )
        assert cb.is_open("a") is True
        assert cb.is_open("b") is False

    def test_should_allow_check(self) -> None:
        cb = self._make_cb(failure_threshold=3)
        assert cb.should_allow_check("c") is True

    def test_should_not_allow_check_when_open(self) -> None:
        cb = self._make_cb(failure_threshold=3)
        for _ in range(3):
            cb._tracker.record_check(
                HealthCheckResult(component="c", ok=False, detail="fail")
            )
        assert cb.should_allow_check("c") is False
