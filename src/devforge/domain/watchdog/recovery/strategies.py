#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/recovery/*, application/watchdog_service.py
"""Recovery strategies and protocols."""
from __future__ import annotations

from typing import Protocol

from devforge.domain.watchdog.model import ComponentState, ComponentStatus, RecoveryAction


class RecoveryStrategy(Protocol):
    """Protocol for component recovery strategies."""

    def can_handle(self, component: str, status: ComponentStatus) -> bool: ...
    def recover(self, component: str, status: ComponentStatus) -> RecoveryAction: ...


class GraduatedRecoveryStrategy:
    """Graduated recovery: escalate action based on failure count."""

    def __init__(self) -> None:
        self._escalation_levels = [
            (1, "soft", "Soft restart"),
            (3, "medium", "Service restart"),
            (5, "hard", "Hard reset + dependency restart"),
        ]

    def can_handle(self, component: str, status: ComponentStatus) -> bool:
        return status.state in (ComponentState.DEGRADED, ComponentState.CRITICAL)

    def recover(self, component: str, status: ComponentStatus) -> RecoveryAction:
        severity = self._determine_severity(status.fail_count)
        action_type = self._map_action(severity)
        return RecoveryAction(
            component=component,
            action_type=action_type,
            detail=f"{action_type.title()} recovery (fail_count={status.fail_count})",
            severity=severity,
        )

    def _determine_severity(self, fail_count: int) -> int:
        for threshold, level, _ in reversed(self._escalation_levels):
            if fail_count >= threshold:
                return ["soft", "medium", "hard"].index(level)
        return 0

    def _map_action(self, severity: int) -> str:
        mapping = {0: "restart", 1: "reload", 2: "reset"}
        return mapping.get(severity, "escalate")
