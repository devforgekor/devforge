#!/usr/bin/env python3.12
# Status: experimental
# Path: application/controllers.py (CatchupController)
"""Catch-up adapter (B): systemctl --user start (oneshot / timer's service)."""

from __future__ import annotations

import asyncio
import subprocess

from devforge.ports.catchup import CatchupPort


class SystemdCatchupAdapter(CatchupPort):
    async def _systemctl(self, *args: str) -> bool:
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["systemctl", "--user", *args], capture_output=True, timeout=60
            )
            return r.returncode == 0
        except Exception:  # noqa: BLE001 — non-zero/timeout is a failed catch-up, not a crash
            return False

    async def run_oneshot(self, unit: str) -> bool:
        return await self._systemctl("start", unit)

    async def kick_timer(self, timer: str) -> bool:
        # Kick the timer's target service (guide §4); the timer unit itself stays.
        return await self._systemctl("start", timer.replace(".timer", ".service"))
