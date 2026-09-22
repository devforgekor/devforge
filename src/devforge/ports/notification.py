#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/notification/*, application/watchdog_service.py
"""Port: notification delivery."""
from __future__ import annotations

from typing import Protocol

from devforge.ports.types import ComponentStatus, RecoveryAction


class NotificationPort(Protocol):
    """Port for sending notifications."""

    async def send_alert(self, component: str, status: ComponentStatus) -> bool: ...
    async def send_recovery(
        self, component: str, action: RecoveryAction, success: bool
    ) -> bool: ...
