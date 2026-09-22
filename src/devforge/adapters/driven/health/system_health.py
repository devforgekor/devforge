#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/*, application/watchdog_service.py
"""System memory and disk health check adapters."""
from __future__ import annotations

import psutil

from devforge.domain.watchdog.model import HealthCheckResult


class MemoryHealthCheck:
    """Health check for system memory (async-compatible)."""

    def __init__(self, threshold_percent: float = 90.0) -> None:
        self._threshold = threshold_percent

    async def check_health(self) -> HealthCheckResult:
        mem = psutil.virtual_memory()
        usage_pct = mem.percent
        if usage_pct >= self._threshold:
            return HealthCheckResult(
                component="memory",
                ok=False,
                detail=f"Memory usage {usage_pct:.1f}% >= {self._threshold}%",
            )
        return HealthCheckResult(
            component="memory",
            ok=True,
            detail=f"Memory usage {usage_pct:.1f}%",
        )


class DiskHealthCheck:
    """Health check for disk space (async-compatible)."""

    def __init__(self, path: str, threshold_percent: float = 90.0) -> None:
        self._path = path
        self._threshold = threshold_percent

    async def check_health(self) -> HealthCheckResult:
        try:
            disk = psutil.disk_usage(self._path)
            usage_pct = disk.percent
            if usage_pct >= self._threshold:
                return HealthCheckResult(
                    component=f"disk-{self._path}",
                    ok=False,
                    detail=f"Disk usage {usage_pct:.1f}% >= {self._threshold}%",
                )
            return HealthCheckResult(
                component=f"disk-{self._path}",
                ok=True,
                detail=f"Disk usage {usage_pct:.1f}%",
            )
        except OSError as e:
            return HealthCheckResult(
                component=f"disk-{self._path}",
                ok=False,
                detail=f"Disk check error: {e}",
            )
