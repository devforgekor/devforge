#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""LLM inference probes (legacy checker.py:79-127, 493-528)."""
from __future__ import annotations

from typing import Callable, Mapping, Optional

import httpx

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

TRANSIENT_TOKENS = ("503", "loading model")


class LLMHealthChecker(HealthCheckPort):
    def __init__(self, targets: Mapping[str, int], timeout: int = 60,
                 day_ports: Optional[set[int]] = None,
                 mode_reader: Optional[Callable[[], str]] = None) -> None:
        self._targets = dict(targets)          # label -> port
        self._timeout = timeout
        self._day_ports = day_ports or set()
        self._mode_reader = mode_reader or (lambda: "day")

    async def check_health(self) -> list[HealthCheck]:
        mode = self._mode_reader()
        checks: list[HealthCheck] = []
        for label, port in self._targets.items():
            if mode == "day" and self._day_ports and port not in self._day_ports:
                continue                        # night-only port, skip in day
            checks.append(await self._probe(label, port))
        return checks

    async def _probe(self, label: str, port: int) -> HealthCheck:
        component = f"llm:{label}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                t1 = await client.get(f"http://127.0.0.1:{port}/health")
                if t1.status_code != 200:
                    detail = f"HTTP {t1.status_code}"
                    return self._result(component, False, detail)
                t2 = await client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 1, "temperature": 0.1, "stream": False},
                )
                if t2.status_code == 200 and t2.json().get("choices"):
                    return self._result(component, True, "probe ok")
                return self._result(component, False, "bad probe response")
        except Exception as e:  # noqa: BLE001
            detail = str(e)
            if any(tok in detail.lower() for tok in TRANSIENT_TOKENS):
                return self._result(component, True, f"transient: {detail}")  # not a fault
            return self._result(component, False, detail)

    @staticmethod
    def _result(component: str, ok: bool, detail: str) -> HealthCheck:
        return HealthCheck(component=component, is_healthy=ok, detail=detail)
