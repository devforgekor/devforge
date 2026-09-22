#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/notification/*, application/watchdog_service.py
"""Systemd sd_notify notification adapter."""
from __future__ import annotations

import subprocess

from devforge.domain.watchdog.model import ComponentStatus, RecoveryAction


class SystemdNotifier:
    """Send notifications via systemd sd_notify (async-compatible)."""

    async def send_alert(self, component: str, status: ComponentStatus) -> bool:
        message = f"STATUS=ALERT: {component} ({status.state.value})"
        return self._notify(message)

    async def send_recovery(
        self, component: str, action: RecoveryAction, success: bool
    ) -> bool:
        result = "SUCCESS" if success else "FAILED"
        message = f"STATUS=RECOVERY: {component} {action.action_type} {result}"
        return self._notify(message)

    def _notify(self, message: str) -> bool:
        try:
            result = subprocess.run(
                ["systemd-notify", "--user", message],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
