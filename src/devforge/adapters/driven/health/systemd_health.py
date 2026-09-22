#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/*, application/watchdog_service.py
"""Systemd service and timer health check adapters."""
from __future__ import annotations

import subprocess

from devforge.domain.watchdog.model import HealthCheckResult


class SystemdServiceHealthCheck:
    """Health check for systemd user services (async-compatible)."""

    def __init__(self, service_names: list[str]) -> None:
        self._services = service_names

    async def check_health(self) -> HealthCheckResult:
        failed = []
        for service in self._services:
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "is-active", service],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    failed.append(service)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                failed.append(service)

        if failed:
            return HealthCheckResult(
                component="systemd-services",
                ok=False,
                detail=f"Failed services: {', '.join(failed)}",
            )
        return HealthCheckResult(
            component="systemd-services",
            ok=True,
            detail=f"All {len(self._services)} services active",
        )


class SystemdTimerHealthCheck:
    """Health check for systemd timers (async-compatible)."""

    def __init__(self, timer_names: list[str]) -> None:
        self._timers = timer_names

    async def check_health(self) -> HealthCheckResult:
        failed = []
        for timer in self._timers:
            try:
                result = subprocess.run(
                    ["systemctl", "--user", "is-active", timer],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    failed.append(timer)
            except (subprocess.TimeoutExpired, FileNotFoundError):
                failed.append(timer)

        if failed:
            return HealthCheckResult(
                component="systemd-timers",
                ok=False,
                detail=f"Failed timers: {', '.join(failed)}",
            )
        return HealthCheckResult(
            component="systemd-timers",
            ok=True,
            detail=f"All {len(self._timers)} timers active",
        )
