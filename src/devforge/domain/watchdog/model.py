#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/*, adapters/driven/health/*, application/watchdog_service.py
"""Core domain types for watchdog subsystem (re-exports from ports.types)."""
from __future__ import annotations

from devforge.ports.types import (  # noqa: F401
    ComponentState,
    ComponentStatus,
    HealthCheckResult,
    Incident,
    RecoveryAction,
)

__all__ = [
    "ComponentState",
    "ComponentStatus",
    "HealthCheckResult",
    "Incident",
    "RecoveryAction",
]
