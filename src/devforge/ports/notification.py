#!/usr/bin/env python3
# Status: experimental
# Path: ports/notification.py
"""Notification port (legacy notifier.py:345, 362)."""

from __future__ import annotations

from typing import Protocol


class NotificationPort(Protocol):
    async def send_alert(self, component: str, state: str, detail: str) -> bool: ...
    async def send_recovery(self, component: str, detail: str) -> bool: ...
    async def sd_notify(self, state: str) -> bool: ...
