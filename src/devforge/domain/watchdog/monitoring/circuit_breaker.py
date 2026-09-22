#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/monitoring/*, application/watchdog_service.py
"""Circuit breaker pattern for component protection."""
from __future__ import annotations

from datetime import datetime, timezone

from devforge.domain.watchdog.model import ComponentStatus
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker


class CircuitBreaker:
    """Circuit breaker pattern for component protection.

    Prevents cascading failures by blocking operations when
    a component is in CRITICAL state.
    """

    def __init__(self, tracker: ComponentTracker) -> None:
        self._tracker = tracker

    def is_open(self, component: str) -> bool:
        status = self._tracker.get_status(component)
        return status is not None and status.circuit_open

    def should_allow_check(self, component: str) -> bool:
        return not self.is_open(component)

    def should_allow_recovery(self, component: str) -> bool:
        status = self._tracker.get_status(component)
        if status is None:
            return True
        return not status.circuit_open or self._should_attempt_reset(status)

    def _should_attempt_reset(self, status: ComponentStatus) -> bool:
        if not status.circuit_open:
            return False
        if status.last_failure is None:
            return True
        elapsed = datetime.now(timezone.utc) - status.last_failure
        return elapsed.total_seconds() > 300
