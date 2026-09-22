#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/recovery/
"""RecoveryPort implementation (legacy recover_* family)."""
from __future__ import annotations

import asyncio
import subprocess
from typing import Callable, Optional

from devforge.ports.recovery import RecoveryPort
from devforge.ports.types import RecoveryAction

_RESTART = ("service", "container")
_PROBE_DELAY_SEC = 5.0   # legacy recovery.py:111 sleep(5)


class SystemdRecoveryAdapter(RecoveryPort):
    """Executes a RecoveryAction via systemctl; optional post-restart probe."""

    def __init__(self, probe: Optional[Callable[[str], "asyncio.Future[bool]"]] = None) -> None:
        self._probe = probe

    async def _systemctl(self, *args: str) -> bool:
        try:
            r = await asyncio.to_thread(subprocess.run, ["systemctl", "--user", *args],
                                        capture_output=True, timeout=30)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        unit = action.component.split(":", 1)[1] if ":" in action.component else action.component
        if action.kind in _RESTART:
            ok = await self._systemctl("restart", unit)
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
            try:
                ok = bool(await self._probe(action.component))
            except Exception:  # noqa: BLE001
                ok = False
        return ok
