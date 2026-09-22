#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""Systemd notification adapter (async via sd_notify)."""
from __future__ import annotations

import asyncio
import subprocess

from devforge.domain.watchdog.model import ComponentStatus, RecoveryAction
from devforge.ports.notification import NotificationPort


class SystemdNotifier(NotificationPort):
    """Send notifications via systemd sd_notify."""

    async def send_alert(self, component: str, status: ComponentStatus) -> bool:
        """Send alert via sd_notify."""
        message = f"STATUS=ALERT: {component} ({status.state.value})"
        return await self._notify(message)

    async def send_recovery(
        self, component: str, action: RecoveryAction, success: bool
    ) -> bool:
        """Send recovery notification via sd_notify."""
        result = "SUCCESS" if success else "FAILED"
        message = f"STATUS=RECOVERY: {component} {action.action_type} {result}"
        return await self._notify(message)

    async def _notify(self, message: str) -> bool:
        """Execute systemd-notify."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemd-notify", "--user", message],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False
