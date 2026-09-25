#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""systemd user service/timer checks (legacy checker.py:150-162, 371-397)."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from typing import Iterable, Mapping

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

DEFAULT_TIMER_MAX_IDLE_SEC = 2100


async def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=5)


class SystemdServiceHealthChecker(HealthCheckPort):
    def __init__(self, services: Iterable[str], prefix: str = "svc") -> None:
        self._services = list(services)
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name) for name in self._services]

    async def _check(self, name: str) -> HealthCheck:
        try:
            r = await _run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    name,
                    "--property=LoadState",
                    "--property=Type",
                    "--property=ActiveState",
                ]
            )
            props: dict[str, str] = {}
            for line in (r.stdout or "").splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
            load = props.get("LoadState", "")
            unit_type = props.get("Type", "")
            state = props.get("ActiveState", "")
            if load and load != "loaded":
                ok, detail = False, f"load={load} state={state or 'unknown'}"
            else:
                # [WHY] systemd: oneshot은 ExecStart가 끝나면 즉시 inactive가 정상
                #      상태다(systemd#18949). Prometheus 표준 규칙·monitord도
                #      oneshot의 inactive를 무시한다. 장기 러닝 유닛의 inactive는
                #      장애로 본다(systemd: inactive = stopped).
                ok = state in ("active", "activating", "reloading") or (
                    state == "inactive" and unit_type == "oneshot"
                )
                detail = f"{state or 'unknown'} type={unit_type or '?'}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e) or type(e).__name__
        return HealthCheck(component=f"{self._prefix}:{name}", is_healthy=ok, detail=detail)


class SystemdTimerHealthChecker(HealthCheckPort):
    def __init__(self, timers: Mapping[str, int], prefix: str = "timer") -> None:
        self._timers = dict(timers)  # timer unit -> max_idle_sec
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name, max_idle) for name, max_idle in self._timers.items()]

    async def _check(self, name: str, max_idle: int) -> HealthCheck:
        try:
            # [WHY] legacy check_timer(): LastTriggerUSec는 타이머 본체 발동 시각만
            #      갱신한다. oneshot 서비스는 종료 후 ActiveEnterTimestamp가 비므로
            #      서비스 ExecMainStartTimestamp까지 함께 봐야 수동 kick도 잡는다.
            svc_name = name.replace(".timer", ".service")
            queries = [
                (name, "LastTriggerUSec"),
                (svc_name, "ActiveEnterTimestamp"),
                (svc_name, "ExecMainStartTimestamp"),
            ]
            candidates: list[datetime] = []
            for unit, prop in queries:
                r = await _run(
                    ["systemctl", "--user", "show", unit, f"--property={prop}", "--value"]
                )
                s = (r.stdout or "").strip()
                if not s or s == "n/a":
                    continue
                try:
                    candidates.append(
                        datetime.strptime(s, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
                    )
                except ValueError:
                    pass
            if not candidates:
                return HealthCheck(f"{self._prefix}:{name}", False, "never triggered")
            last_dt = max(candidates)
            idle = (datetime.now(timezone.utc) - last_dt).total_seconds()
            ok = idle <= max_idle
            detail = f"{int(idle)}s ago" if ok else f"{int(idle)}s idle > {max_idle}s limit"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e) or type(e).__name__
        return HealthCheck(f"{self._prefix}:{name}", ok, detail)


class SystemdSystemServiceHealthChecker(HealthCheckPort):
    """Rootful system services — alert-only (legacy checker.py:583-596)."""

    def __init__(self, services: Iterable[str], prefix: str = "syssvc") -> None:
        self._services = list(services)
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name) for name in self._services]

    async def _check(self, name: str) -> HealthCheck:
        try:
            r = await _run(["systemctl", "is-active", name])  # no --user (rootful scope)
            st = r.stdout.strip()
            ok = st == "active"
            detail = st or "unknown"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        return HealthCheck(component=f"{self._prefix}:{name}", is_healthy=ok, detail=detail)


class OneshotResultHealthChecker(HealthCheckPort):
    """One-shot service last-run result (legacy checker.py:550-573).

    Timer LastTrigger updates even when the service fails, so use
    ActiveState/Result to catch failures (e.g. daily-structure git backlog).
    """

    def __init__(self, services: Iterable[str], prefix: str = "oneshot") -> None:
        self._services = list(services)
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name) for name in self._services]

    async def _check(self, name: str) -> HealthCheck:
        try:
            r = await _run(
                ["systemctl", "--user", "show", name, "--property=ActiveState", "--property=Result"]
            )
            props: dict[str, str] = {}
            for line in r.stdout.strip().splitlines():
                if "=" in line:
                    k, _, v = line.partition("=")
                    props[k.strip()] = v.strip()
            active = props.get("ActiveState", "")
            result = props.get("Result", "")
            ok = not (active == "failed" or result not in ("", "success"))
            detail = f"ActiveState={active} Result={result or 'success'}"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        return HealthCheck(component=f"{self._prefix}:{name}", is_healthy=ok, detail=detail)
