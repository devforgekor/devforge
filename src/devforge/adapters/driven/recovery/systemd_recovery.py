#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/recovery/*, application/watchdog_service.py
"""Systemd recovery action adapter."""
from __future__ import annotations

import subprocess

from devforge.domain.watchdog.model import RecoveryAction


class SystemdRecoveryAdapter:
    """Execute recovery actions via systemd (async-compatible)."""

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        if action.action_type == "restart":
            return self._restart_service(action.component)
        elif action.action_type == "reload":
            return self._reload_service(action.component)
        elif action.action_type == "reset":
            return self._hard_reset(action.component)
        return False

    def _restart_service(self, service: str) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", service],
                capture_output=True, timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _reload_service(self, service: str) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "reload-or-restart", service],
                capture_output=True, timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _hard_reset(self, service: str) -> bool:
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", service],
                capture_output=True, timeout=30,
            )
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True, timeout=30,
            )
            result = subprocess.run(
                ["systemctl", "--user", "start", service],
                capture_output=True, timeout=30,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
