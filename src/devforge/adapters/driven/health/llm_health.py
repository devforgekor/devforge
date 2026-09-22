#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/*, application/watchdog_service.py
"""LLM inference pod health check adapter."""
from __future__ import annotations

import httpx

from devforge.domain.watchdog.model import HealthCheckResult


class LLMHealthCheck:
    """Health check for LLM inference pods (async via httpx)."""

    def __init__(self, targets: dict[str, int], timeout: int = 10) -> None:
        self._targets = targets  # {"pod-a": 11434, "pod-b": 11435}
        self._timeout = timeout

    async def check_health(self) -> HealthCheckResult:
        failed = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for pod_name, port in self._targets.items():
                if not await self._probe_pod(client, pod_name, port):
                    failed.append(pod_name)

        if failed:
            return HealthCheckResult(
                component="llm-pods",
                ok=False,
                detail=f"Failed pods: {', '.join(failed)}",
            )
        return HealthCheckResult(
            component="llm-pods",
            ok=True,
            detail=f"All {len(self._targets)} pods responding",
        )

    async def _probe_pod(
        self, client: httpx.AsyncClient, pod_name: str, port: int
    ) -> bool:
        try:
            resp = await client.get(f"http://localhost:{port}/health")
            return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
