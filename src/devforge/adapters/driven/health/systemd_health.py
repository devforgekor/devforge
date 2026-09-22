#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""Systemd health check adapter (async)."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheckResult


class SystemdServiceHealthCheck(HealthCheckPort):
    """Health check for systemd user services."""

    def __init__(self, service_names: list[str]) -> None:
        self._services = service_names

    async def check_health(self) -> HealthCheckResult:
        """Check if all services are active."""
        failed = []

        for service in self._services:
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "--user", "is-active", service],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    failed.append(service)
            except Exception:
                failed.append(service)

        if failed:
            return HealthCheckResult(
                component="systemd-services",
                ok=False,
                detail=f"Failed services: {', '.join(failed)}",
                timestamp=datetime.now(timezone.utc),
            )

        return HealthCheckResult(
            component="systemd-services",
            ok=True,
            detail=f"All {len(self._services)} services active",
            timestamp=datetime.now(timezone.utc),
        )


class SystemdTimerHealthCheck(HealthCheckPort):
    """Health check for systemd timers."""

    def __init__(self, timer_names: list[str]) -> None:
        self._timers = timer_names

    async def check_health(self) -> HealthCheckResult:
        """Check if all timers are active."""
        failed = []

        for timer in self._timers:
            try:
                result = await asyncio.to_thread(
                    subprocess.run,
                    ["systemctl", "--user", "is-active", timer],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode != 0:
                    failed.append(timer)
            except Exception:
                failed.append(timer)

        if failed:
            return HealthCheckResult(
                component="systemd-timers",
                ok=False,
                detail=f"Failed timers: {', '.join(failed)}",
                timestamp=datetime.now(timezone.utc),
            )

        return HealthCheckResult(
            component="systemd-timers",
            ok=True,
            detail=f"All {len(self._timers)} timers active",
            timestamp=datetime.now(timezone.utc),
        )
