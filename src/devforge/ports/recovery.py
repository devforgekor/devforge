#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/recovery/*, application/watchdog_service.py
"""Port: recovery action execution."""
from __future__ import annotations

from typing import Protocol

from devforge.ports.types import RecoveryAction


class RecoveryPort(Protocol):
    """Port for recovery action execution."""

    async def execute_recovery(self, action: RecoveryAction) -> bool: ...
