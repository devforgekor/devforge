#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/orchestration/*, application/watchdog_service.py
"""Check coordination logic (pure, no I/O)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheckResult


@dataclass
class CheckPlan:
    """Plan for executing health checks."""
    components: list[str]
    parallel: bool = True
    timeout_per_check: int = 30


class CheckCoordinator:
    """Coordinate health check execution across components.

    Pure orchestration logic — delegates actual checks to adapters via ports.
    """

    def __init__(
        self,
        tracker: ComponentTracker,
        health_ports: Mapping[str, HealthCheckPort],
    ) -> None:
        self._tracker = tracker
        self._health_ports = health_ports

    async def execute_checks(self, plan: CheckPlan) -> list[HealthCheckResult]:
        results = []
        for component in plan.components:
            port = self._health_ports.get(component)
            if port is None:
                results.append(
                    HealthCheckResult(
                        component=component,
                        ok=False,
                        detail="No health check port registered",
                    )
                )
                continue
            result = await port.check_health()
            self._tracker.record_check(result)
            results.append(result)
        return results

    def should_skip_check(self, component: str) -> bool:
        status = self._tracker.get_status(component)
        if status is None:
            return False
        return status.circuit_open
