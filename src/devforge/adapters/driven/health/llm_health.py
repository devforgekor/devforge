#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""LLM inference probes (legacy checker.py:79-127, 493-528)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Optional

import httpx

from devforge.core.config import RUNTIME_ENV_FILE, SYSTEM_ENV_FILE
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

TRANSIENT_TOKENS = ("503", "loading model")
DEFAULT_LATENCY_BASELINE_MS = 2000


def _read_key(path: Path, key: str) -> str:
    try:
        with open(path) as f:
            for line in f:
                if line.startswith(key + "="):
                    return line.strip().split("=", 1)[1]
    except OSError:
        pass
    return ""


def read_runtime_mode() -> str:
    """[WHY] legacy read_mode(): 매 사이클 current-system-mode.env를 다시 읽는다."""
    return _read_key(SYSTEM_ENV_FILE, "MODE") or "day"


def read_serving_port() -> Optional[int]:
    """[WHY] legacy _current_inference_port(): 추론 컨테이너는 한 포트만 서빙하므로
    모드 전환 사이에 다른 포트를 프로브하면 오탐이 난다."""
    raw = _read_key(RUNTIME_ENV_FILE, "PORT")
    try:
        return int(raw)
    except ValueError:
        return None


class LLMHealthChecker(HealthCheckPort):
    def __init__(
        self,
        targets: Mapping[str, int],
        timeout: int = 60,
        day_ports: Optional[set[int]] = None,
        mode_reader: Optional[Callable[[], str]] = None,
        latency_baseline_ms: int = DEFAULT_LATENCY_BASELINE_MS,
        serving_port_reader: Optional[Callable[[], Optional[int]]] = None,
    ) -> None:
        self._targets = dict(targets)
        self._timeout = timeout
        self._day_ports = day_ports or set()
        self._mode_reader = mode_reader or (lambda: "day")
        self._latency_baseline_ms = latency_baseline_ms
        self._serving_port_reader = serving_port_reader

    async def check_health(self) -> list[HealthCheck]:
        mode = self._mode_reader()
        serving = self._serving_port_reader() if self._serving_port_reader else None
        checks: list[HealthCheck] = []
        for label, port in self._targets.items():
            if mode == "day" and self._day_ports and port not in self._day_ports:
                continue  # night-only port, skip in day
            if serving is not None and port != serving:
                continue  # not serving right now — legacy skips to avoid false alerts
            checks.append(await self._probe(label, port))
        return checks

    async def _probe(self, label: str, port: int) -> HealthCheck:
        component = f"llm:{label}"
        threshold = float(self._latency_baseline_ms * 3)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                t1 = await client.get(f"http://127.0.0.1:{port}/health")
                if t1.status_code != 200:
                    detail = f"HTTP {t1.status_code}"
                    return self._result(component, False, detail)
                start = time.monotonic()
                t2 = await client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={
                        "messages": [{"role": "user", "content": "hi"}],
                        "max_tokens": 1,
                        "temperature": 0.1,
                        "stream": False,
                    },
                )
                latency_ms = (time.monotonic() - start) * 1000
                if t2.status_code == 200 and t2.json().get("choices"):
                    return self._result(
                        component,
                        True,
                        f"probe ok ({int(latency_ms)}ms)",
                        metric_value=latency_ms,
                        threshold=threshold,
                    )
                return self._result(component, False, "bad probe response")
        except Exception as e:  # noqa: BLE001
            # [WHY] httpx.ReadError('')처럼 빈 메시지 예외가 있어 type 이름으로 보강한다.
            detail = str(e) or type(e).__name__
            if any(tok in detail.lower() for tok in TRANSIENT_TOKENS):
                return self._result(component, True, f"transient: {detail}")  # not a fault
            return self._result(component, False, detail)

    @staticmethod
    def _result(
        component: str,
        ok: bool,
        detail: str,
        metric_value: float | None = None,
        threshold: float | None = None,
    ) -> HealthCheck:
        return HealthCheck(
            component=component,
            is_healthy=ok,
            detail=detail,
            metric_value=metric_value,
            threshold=threshold,
        )
