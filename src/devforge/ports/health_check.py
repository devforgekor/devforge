#!/usr/bin/env python3
# Status: experimental
# Path: ports/health_check.py
"""Health check port (batch)."""

from __future__ import annotations

from typing import Protocol

from devforge.ports.types import HealthCheck


class HealthCheckPort(Protocol):
    async def check_health(self) -> list[HealthCheck]: ...
