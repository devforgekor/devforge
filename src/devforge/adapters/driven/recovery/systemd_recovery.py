#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""Systemd recovery adapter (async)."""
from __future__ import annotations

import asyncio
import subprocess

from devforge.ports.recovery import RecoveryPort
from devforge.ports.types import RecoveryAction


class SystemdRecoveryAdapter(RecoveryPort):
    """Execute recovery actions via systemd."""

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        """Execute recovery action for systemd service."""
        if action.action_type == "restart":
            return await self._restart_service(action.component)
        elif action.action_type == "reload":
            return await self._reload_service(action.component)
        elif action.action_type == "reset":
            return await self._hard_reset(action.component)
        else:
            return False

    async def _restart_service(self, service: str) -> bool:
        """Soft restart: systemctl restart."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "restart", service],
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _reload_service(self, service: str) -> bool:
        """Medium recovery: systemctl reload-or-restart."""
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "reload-or-restart", service],
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _hard_reset(self, service: str) -> bool:
        """Hard reset: stop + daemon-reload + start."""
        try:
            # Stop
            await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "stop", service],
                capture_output=True,
                timeout=30,
            )

            # Daemon reload
            await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
                timeout=30,
            )

            # Start
            result = await asyncio.to_thread(
                subprocess.run,
                ["systemctl", "--user", "start", service],
                capture_output=True,
                timeout=30,
            )
            return result.returncode == 0
        except Exception:
            return False
