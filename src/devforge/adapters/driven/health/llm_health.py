#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""LLM health check adapter (async via httpx)."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheckResult


class LLMHealthCheck(HealthCheckPort):
    """Health check for LLM inference pods."""

    def __init__(self, targets: dict[str, int], timeout: int = 10) -> None:
        self._targets = targets  # {"pod-a": 11434, "pod-b": 11435}
        self._timeout = timeout

    async def check_health(self) -> HealthCheckResult:
        """Check if all LLM pods are responding."""
        failed = []

        for pod_name, port in self._targets.items():
            if not await self._probe_pod(pod_name, port):
                failed.append(pod_name)

        if failed:
            return HealthCheckResult(
                component="llm-pods",
                ok=False,
                detail=f"Failed pods: {', '.join(failed)}",
                timestamp=datetime.now(timezone.utc),
            )

        return HealthCheckResult(
            component="llm-pods",
            ok=True,
            detail=f"All {len(self._targets)} pods responding",
            timestamp=datetime.now(timezone.utc),
        )

    async def _probe_pod(self, pod_name: str, port: int) -> bool:
        """Probe single LLM pod via /api/tags."""
        try:
            response = await httpx.get(  # type: ignore[misc]
                f"http://localhost:{port}/api/tags",
                timeout=self._timeout,
            )
            return bool(response.status_code == 200)
        except Exception:
            return False
