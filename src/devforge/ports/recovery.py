#!/usr/bin/env python3
# Status: experimental
# Path: ports/recovery.py
"""Recovery port (legacy recover_* family)."""

from __future__ import annotations

from typing import Protocol

from devforge.ports.types import RecoveryAction


class RecoveryPort(Protocol):
    async def execute_recovery(self, action: RecoveryAction) -> bool: ...
