#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/monitoring/*, application/watchdog_service.py
"""Component health tracker with circuit breaker pattern."""
from __future__ import annotations

from devforge.domain.watchdog.model import ComponentState, ComponentStatus, HealthCheckResult


class TrendTracker:
    """Track failure trends for circuit breaker decisions (pure, no I/O)."""

    def __init__(self, window_size: int = 10) -> None:
        self._window_size = window_size
        self._history: dict[str, list[bool]] = {}

    def record(self, component: str, success: bool) -> None:
        if component not in self._history:
            self._history[component] = []
        self._history[component].append(success)
        if len(self._history[component]) > self._window_size:
            self._history[component].pop(0)

    def failure_rate(self, component: str) -> float:
        if component not in self._history or not self._history[component]:
            return 0.0
        failures = sum(1 for ok in self._history[component] if not ok)
        return failures / len(self._history[component])


class ComponentTracker:
    """Track component health status and circuit breaker state.

    Pure domain logic — no I/O, no side effects.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        success_threshold: int = 2,
    ) -> None:
        self._threshold_fail = failure_threshold
        self._threshold_success = success_threshold
        self._states: dict[str, ComponentStatus] = {}
        self._trend = TrendTracker()

    def record_check(self, result: HealthCheckResult) -> ComponentStatus:
        current = self._states.get(result.component)
        if current is None:
            return self._initialize_component(result)
        return self._transition(current, result)

    def _initialize_component(self, result: HealthCheckResult) -> ComponentStatus:
        status = ComponentStatus(
            name=result.component,
            state=ComponentState.HEALTHY if result.ok else ComponentState.DEGRADED,
            fail_count=0 if result.ok else 1,
            consecutive_failures=0 if result.ok else 1,
            circuit_open=False,
            last_success=result.timestamp if result.ok else None,
            last_failure=None if result.ok else result.timestamp,
        )
        self._states[result.component] = status
        self._trend.record(result.component, result.ok)
        return status

    def _transition(
        self, current: ComponentStatus, result: HealthCheckResult
    ) -> ComponentStatus:
        self._trend.record(result.component, result.ok)
        if result.ok:
            return self._handle_success(current, result)
        return self._handle_failure(current, result)

    def _handle_success(
        self, current: ComponentStatus, result: HealthCheckResult
    ) -> ComponentStatus:
        new_state = current.state
        circuit_open = current.circuit_open

        if current.state == ComponentState.RECOVERING and current.consecutive_failures == 0:
                consecutive_success = self._count_recent_success(result.component)
                if consecutive_success >= self._threshold_success:
                    new_state = ComponentState.HEALTHY
                    circuit_open = False

        updated = ComponentStatus(
            name=current.name,
            state=new_state,
            fail_count=current.fail_count,
            consecutive_failures=0,
            circuit_open=circuit_open,
            last_success=result.timestamp,
            last_failure=current.last_failure,
        )
        self._states[result.component] = updated
        return updated

    def _handle_failure(
        self, current: ComponentStatus, result: HealthCheckResult
    ) -> ComponentStatus:
        new_fail_count = current.fail_count + 1
        new_consecutive = current.consecutive_failures + 1
        new_state = current.state
        circuit_open = current.circuit_open

        if new_consecutive >= self._threshold_fail:
            if current.state == ComponentState.HEALTHY:
                new_state = ComponentState.DEGRADED
            elif current.state == ComponentState.DEGRADED:
                new_state = ComponentState.CRITICAL
                circuit_open = True

        updated = ComponentStatus(
            name=current.name,
            state=new_state,
            fail_count=new_fail_count,
            consecutive_failures=new_consecutive,
            circuit_open=circuit_open,
            last_success=current.last_success,
            last_failure=result.timestamp,
        )
        self._states[result.component] = updated
        return updated

    def _count_recent_success(self, component: str) -> int:
        if component not in self._trend._history:
            return 0
        history = self._trend._history[component]
        count = 0
        for ok in reversed(history):
            if ok:
                count += 1
            else:
                break
        return count

    def get_status(self, component: str) -> ComponentStatus | None:
        return self._states.get(component)

    def all_statuses(self) -> list[ComponentStatus]:
        return list(self._states.values())
