#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/notification/
"""sd_notify notifier (legacy notifier.py:26-48)."""

from __future__ import annotations

import asyncio
import subprocess

from devforge.ports.notification import NotificationPort


class SystemdNotifier(NotificationPort):
    async def sd_notify(self, state: str) -> bool:
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["systemd-notify", "--user", state], capture_output=True, timeout=5
            )
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def send_alert(self, component: str, state: str, detail: str) -> bool:
        return await self.sd_notify(f"STATUS=ALERT {component} {state} {detail}")

    async def send_recovery(self, component: str, detail: str) -> bool:
        return await self.sd_notify(f"STATUS=RECOVERED {component} {detail}")
