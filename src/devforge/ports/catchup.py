#!/usr/bin/env python3.12
# Status: experimental
# Path: application/controllers.py (via cli.py / watchdog_service)
"""Catch-up port (B logic) — run a missed oneshot / kick a stale timer."""

from __future__ import annotations

from typing import Protocol


class CatchupPort(Protocol):
    async def run_oneshot(self, unit: str) -> bool: ...
    async def kick_timer(self, timer: str) -> bool: ...
