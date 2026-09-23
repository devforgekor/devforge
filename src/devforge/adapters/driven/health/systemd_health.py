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
            r = await _run(["systemctl", "--user", "is-active", name])
            ok = r.returncode == 0
            detail = "active" if ok else "inactive"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        return HealthCheck(component=f"{self._prefix}:{name}", is_healthy=ok, detail=detail)


class SystemdTimerHealthChecker(HealthCheckPort):
    def __init__(self, timers: Mapping[str, int], prefix: str = "timer") -> None:
        self._timers = dict(timers)  # timer unit -> max_idle_sec
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name, max_idle) for name, max_idle in self._timers.items()]

    async def _check(self, name: str, max_idle: int) -> HealthCheck:
        try:
            r = await _run(
                ["systemctl", "--user", "show", name, "--property=LastTriggerUSec", "--value"]
            )
            last = r.stdout.strip()
            if not last or last == "n/a":
                return HealthCheck(f"{self._prefix}:{name}", False, "never triggered")
            last_dt = datetime.strptime(last, "%a %Y-%m-%d %H:%M:%S %Z").replace(
                tzinfo=timezone.utc
            )
            idle = (datetime.now(timezone.utc) - last_dt).total_seconds()
            ok = idle <= max_idle
            detail = f"{int(idle)}s ago" if ok else f"{int(idle)}s idle > {max_idle}s limit"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
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
