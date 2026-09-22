#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""System health check adapter (async via psutil)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import psutil

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheckResult


class MemoryHealthCheck(HealthCheckPort):
    """Health check for system memory."""

    def __init__(self, threshold_percent: float = 90.0) -> None:
        self._threshold = threshold_percent

    async def check_health(self) -> HealthCheckResult:
        """Check if memory usage is below threshold."""
        mem = await asyncio.to_thread(psutil.virtual_memory)
        usage_pct = mem.percent

        if usage_pct >= self._threshold:
            return HealthCheckResult(
                component="memory",
                ok=False,
                detail=f"Memory usage {usage_pct:.1f}% >= {self._threshold}%",
                timestamp=datetime.now(timezone.utc),
            )

        return HealthCheckResult(
            component="memory",
            ok=True,
            detail=f"Memory usage {usage_pct:.1f}%",
            timestamp=datetime.now(timezone.utc),
        )


class DiskHealthCheck(HealthCheckPort):
    """Health check for disk space."""

    def __init__(self, path: str, threshold_percent: float = 90.0) -> None:
        self._path = path
        self._threshold = threshold_percent

    async def check_health(self) -> HealthCheckResult:
        """Check if disk usage is below threshold."""
        disk = await asyncio.to_thread(psutil.disk_usage, self._path)
        usage_pct = disk.percent

        if usage_pct >= self._threshold:
            return HealthCheckResult(
                component=f"disk-{self._path}",
                ok=False,
                detail=f"Disk usage {usage_pct:.1f}% >= {self._threshold}%",
                timestamp=datetime.now(timezone.utc),
            )

        return HealthCheckResult(
            component=f"disk-{self._path}",
            ok=True,
            detail=f"Disk usage {usage_pct:.1f}%",
            timestamp=datetime.now(timezone.utc),
        )
