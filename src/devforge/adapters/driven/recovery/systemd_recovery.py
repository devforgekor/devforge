#!/usr/bin/env python3.12
# Status: experimental
# Path: adapters/driven/recovery/
"""RecoveryPort implementation (legacy recover_* family)."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Awaitable, Callable, Optional

from devforge.ports.recovery import RecoveryPort
from devforge.ports.types import RecoveryAction

_RESTART = ("service", "container")
_PROBE_DELAY_SEC = 5.0  # legacy recovery.py:111 sleep(5)
# [WHY] 복구 후 판정 파라미터는 k8s probe 기본값을 따른다 (failureThreshold=3,
#       periodSeconds=10). 단 한 번의 성공만으로 resolved 처리하지 않는다.
_VERIFY_ATTEMPTS = 3
_VERIFY_INTERVAL_SEC = 10.0
_VERIFY_PASS_REQUIRED = 2


class SystemdRecoveryAdapter(RecoveryPort):
    """Executes a RecoveryAction via systemctl; optional post-restart probe."""

    def __init__(self, probe: Optional[Callable[[str], Awaitable[bool]]] = None) -> None:
        self._probe = probe

    async def _systemctl(self, *args: str) -> bool:
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["systemctl", "--user", *args], capture_output=True, timeout=30
            )
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def _verify(self, component: str) -> bool:
        """독립적인 헬스 재확인 — 명령의 exit code는 증거가 아니다.

        systemd 문서: "systemctl start ... will report success even if the
        service's binary cannot be invoked successfully". 그래서 복구 후 실제
        헬스를 M-of-N consecutive 로 확인한다 (Nagios SOFT→HARD 와 같은 구조).
        """
        if self._probe is None:
            return False
        consecutive = 0
        for attempt in range(_VERIFY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_VERIFY_INTERVAL_SEC)
            try:
                ok = bool(await self._probe(component))
            except Exception:  # noqa: BLE001
                ok = False
            consecutive = consecutive + 1 if ok else 0
            if consecutive >= _VERIFY_PASS_REQUIRED:
                return True
        return False

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        unit = action.component.split(":", 1)[1] if ":" in action.component else action.component
        if action.kind in _RESTART:
            # [WHY] start limit ("Start request repeated too quickly") 으로 park 된 유닛은
            #       카운터가 살아 있어 어떤 재시도도 거부된다. reset-failed 가 유일하게
            #       카운터를 지운다 (systemd.unit(5)). bare restart 는 stop+start 뿐이다.
            await self._systemctl("reset-failed", unit)
            ok = await self._systemctl("start", unit)
        elif action.kind == "timer_kick":
            ok = await self._systemctl("start", unit.replace(".timer", ".service"))
        elif action.kind == "oneshot":
            ok = await self._systemctl("start", unit)
        elif action.kind == "svcpod":
            ok = await self._systemctl("restart", "svc-pod.service")
        elif action.kind == "pipeline":
            ok = await self._systemctl("restart", "devforge-day-cycle.service")
        elif action.kind == "cascade":
            ok = await self._systemctl("restart", "svc-pod.service")
        else:
            return False

        if ok and self._probe is not None:
            await asyncio.sleep(_PROBE_DELAY_SEC)
            ok = await self._verify(action.component)
        return ok
