#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/recovery/
"""Recovery coordinator — legacy graduated_recover (recovery.py:270-297)."""
from __future__ import annotations

from typing import Optional

from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.recovery.strategies import DefaultRecoveryStrategy, RecoveryStrategy
from devforge.ports.types import RecoveryAction


class RecoveryCoordinator:
    def __init__(self, registry: TrackerRegistry,
                 strategy: Optional[RecoveryStrategy] = None) -> None:
        self._registry = registry
        self._strategy = strategy or DefaultRecoveryStrategy()

    def plan(self, component: str, reason: str) -> Optional[RecoveryAction]:
        """Return a RecoveryAction, or None if circuit OPEN / alert-only.

        Mirrors the `if not tracker.can_retry(): return False` gate at
        recovery.py:276-278 and the backoff lookup at recovery.py:280-282.
        """
        t = self._registry.get(component)
        if not t.can_retry():
            return None
        return self._strategy.create_action(component, t.state.value, reason, t.backoff_sec())

    def record_result(self, component: str, succeeded: bool) -> bool:
        """Record success/failure after recovery (recovery.py:288-294)."""
        t = self._registry.get(component)
        if succeeded:
            t.record_success()
            return False
        return t.record_failure()

    def should_escalate(self, component: str) -> bool:
        return self._registry.get(component).state.value == "DOWN"
