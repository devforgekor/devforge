#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/orchestration/
"""Check dispatch (orchestrator.py split)."""

from __future__ import annotations

from collections.abc import Mapping

from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck


class CheckCoordinator:
    def __init__(
        self, registry: TrackerRegistry, health_ports: Mapping[str, HealthCheckPort]
    ) -> None:
        self._registry = registry
        self._ports = health_ports

    async def run(self, groups: list[str] | None = None) -> list[HealthCheck]:
        """Run all (or the named) check groups; record results; return checks."""
        names = groups if groups is not None else list(self._ports)
        results: list[HealthCheck] = []
        for name in names:
            port = self._ports.get(name)
            if port is None:
                continue
            for check in await port.check_health():
                self._registry.get(check.component).record_check(check)
                results.append(check)
        return results

    @staticmethod
    def failed(checks: list[HealthCheck]) -> list[str]:
        return [c.component for c in checks if not c.is_healthy]

    @staticmethod
    def healthy(checks: list[HealthCheck]) -> list[str]:
        return [c.component for c in checks if c.is_healthy]
