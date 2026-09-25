#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""LLM inference probes (legacy checker.py:79-127, 493-528)."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

import httpx

from devforge.core.config import RUNTIME_ENV_FILE, SYSTEM_ENV_FILE
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

TRANSIENT_TOKENS = ("503", "loading model")
DEFAULT_LATENCY_BASELINE_MS = 2000
SLOT_POLL_SEC = 2.0
SATURATION_GRACE_SEC = 15.0


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
        saturation_grace_sec: float = SATURATION_GRACE_SEC,
    ) -> None:
        self._targets = dict(targets)
        self._timeout = timeout
        self._day_ports = day_ports or set()
        self._mode_reader = mode_reader or (lambda: "day")
        self._latency_baseline_ms = latency_baseline_ms
        self._serving_port_reader = serving_port_reader
        self._saturation_grace_sec = saturation_grace_sec

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
                saturated = await self._wait_for_free_slot(client, port)
                if saturated is not None:
                    return self._result(component, True, saturated)
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

    async def _wait_for_free_slot(self, client: httpx.AsyncClient, port: int) -> Optional[str]:
        """[WHY] CPU 전용 llama.cpp(--parallel 2)는 긴 프롬프트(최대 5000tok, 슬롯당
        분 단위)로 두 슬롯을 점유한다. 그러면 1토큰 프로브가 큐에서 60s 타임아웃을
        맞지만 서버는 정상 수행 중이다. 슬롯이 비면 T2를 던지고, 계속 차 있으면
        슬롯 자체가 '실제 추론이 진행 중'인 증거이므로 transient로 본다. /slots를
        읽지 못하면 예전 동작(무조건 T2)으로 돌아간다."""
        deadline = time.monotonic() + self._saturation_grace_sec
        while True:
            slots = await self._read_slots(client, port)
            if slots is None:
                return None
            busy = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
            if busy < len(slots):
                return None
            if time.monotonic() >= deadline:
                return f"transient: {busy}/{len(slots)} slots busy (real work queued)"
            await asyncio.sleep(min(SLOT_POLL_SEC, max(deadline - time.monotonic(), 0.05)))

    async def _read_slots(
        self, client: httpx.AsyncClient, port: int
    ) -> Optional[list[dict[str, Any]]]:
        try:
            r = await client.get(f"http://127.0.0.1:{port}/slots")
            if r.status_code != 200:
                return None
            data = json.loads(r.text)
        except Exception:  # noqa: BLE001 — /slots 미지원·파싱 실패는 예전 경로로 폴백
            return None
        return data if isinstance(data, list) and data else None

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
