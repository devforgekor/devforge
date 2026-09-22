#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/orchestration/*, application/watchdog_service.py
"""Fix coordination logic (pure, no I/O)."""
from __future__ import annotations

from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.recovery import RecoveryPort


class FixCoordinator:
    """Coordinate fix/recovery actions across components (pure, no I/O)."""

    def __init__(
        self,
        recovery_coordinator: RecoveryCoordinator,
        recovery_ports: dict[str, RecoveryPort],
    ) -> None:
        self._recovery = recovery_coordinator
        self._recovery_ports = recovery_ports

    async def execute_fixes(self, failed_components: list[str]) -> dict[str, bool]:
        results = {}
        for component in failed_components:
            action = self._recovery.plan_recovery(component)
            if action is None:
                results[component] = False
                continue
            port = self._recovery_ports.get(component)
            if port is None:
                results[component] = False
                continue
            success = await port.execute_recovery(action)
            results[component] = success
        return results

    def should_escalate(self, component: str) -> bool:
        return self._recovery.should_escalate(component)
