#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/*, application/watchdog_service.py
"""Port: async health check protocol."""
from __future__ import annotations

from typing import Protocol

from devforge.ports.types import HealthCheckResult


class HealthCheckPort(Protocol):
    """Port for health check implementations (async for DB/HTTP I/O)."""

    async def check_health(self) -> HealthCheckResult: ...
