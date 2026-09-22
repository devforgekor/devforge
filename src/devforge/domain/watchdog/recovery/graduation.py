#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/recovery/*, application/watchdog_service.py
"""Recovery coordination logic."""
from __future__ import annotations

from devforge.domain.watchdog.model import ComponentState, RecoveryAction
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.domain.watchdog.recovery.strategies import GraduatedRecoveryStrategy


class RecoveryCoordinator:
    """Coordinate recovery actions based on component health (pure, no I/O)."""

    def __init__(self, tracker: ComponentTracker) -> None:
        self._tracker = tracker
        self._strategies = [GraduatedRecoveryStrategy()]

    def plan_recovery(self, component: str) -> RecoveryAction | None:
        status = self._tracker.get_status(component)
        if status is None:
            return None
        for strategy in self._strategies:
            if strategy.can_handle(component, status):
                return strategy.recover(component, status)
        return None

    def should_escalate(self, component: str) -> bool:
        status = self._tracker.get_status(component)
        if status is None:
            return False
        return status.state == ComponentState.CRITICAL and status.fail_count > 10
