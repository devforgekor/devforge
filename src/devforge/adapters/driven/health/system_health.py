#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""Memory/disk checks (legacy checker.py:306-371)."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Iterable

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

# [WHY] legacy lib/watchdog/config.py 상수와 같아야 한다 — 임계치가 다르면 오탐이
# 난다(2026-09-25 shadow 창에서 swap 1550MB로 74회 오탐, legacy는 all_ok 판정).
# SWAP_CRIT_MB=9000은 이 서버 스왑 총량(4095MB)보다 커 스왑은 사실상 미감시인데,
# 이는 legacy의 미해결 설정이므로 창 종료 후 양쪽 함께 재설계 대상이다.
MEM_WARN_PCT = 80
MEM_CRIT_PCT = 90
SWAP_CRIT_MB = 9000


async def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=5)


class MemoryHealthChecker(HealthCheckPort):
    async def check_health(self) -> list[HealthCheck]:
        try:
            r = await _run(["free", "-m"])
            pct = swap_pct = 0
            swap_used = 0
            for line in r.stdout.splitlines():
                parts = line.split()
                if line.startswith("Mem:"):
                    total, used = int(parts[1]), int(parts[2])
                    pct = round(used / total * 100) if total else 0
                elif line.startswith("Swap:"):
                    total, used = int(parts[1]), int(parts[2])
                    swap_used = used
                    swap_pct = round(used / total * 100) if total else 0
            ok = pct < MEM_CRIT_PCT and swap_used < SWAP_CRIT_MB
            detail = f"mem={pct}% swap={swap_pct}%"
            return [
                HealthCheck(
                    "system:memory",
                    ok,
                    detail,
                    metric_value=float(pct),
                    threshold=float(MEM_CRIT_PCT),
                )
            ]
        except Exception as e:  # noqa: BLE001
            return [HealthCheck("system:memory", False, str(e))]


class DiskHealthChecker(HealthCheckPort):
    def __init__(self, mounts: Iterable[str], threshold_pct: int = 90) -> None:
        self._mounts = list(mounts)
        self._threshold = threshold_pct

    async def check_health(self) -> list[HealthCheck]:
        try:
            r = await _run(["df", "--output=target,pcent", "-x", "tmpfs"])
            usage: dict[str, int] = {}
            for line in r.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    usage[parts[0]] = int(parts[1].replace("%", ""))
            return [self._one(m, usage.get(m)) for m in self._mounts]
        except Exception as e:  # noqa: BLE001
            return [HealthCheck(f"system:disk:{m}", False, str(e)) for m in self._mounts]

    def _one(self, mount: str, pct: int | None) -> HealthCheck:
        if pct is None:
            return HealthCheck(f"system:disk:{mount}", False, "mount not found")
        ok = pct < self._threshold
        return HealthCheck(
            f"system:disk:{mount}",
            ok,
            f"{pct}% used",
            metric_value=float(pct),
            threshold=float(self._threshold),
        )
