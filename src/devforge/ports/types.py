#!/usr/bin/env python3
# Status: experimental
# Path: ports/*, adapters/driven/*, application/*
"""Shared types used by port protocols (ports-layer safe)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ComponentState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    RECOVERING = "recovering"


@dataclass(frozen=True)
class HealthCheckResult:
    """Result of a single health check."""
    component: str
    ok: bool
    detail: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class ComponentStatus:
    """Aggregated status for a tracked component."""
    name: str
    state: ComponentState
    fail_count: int
    consecutive_failures: int
    circuit_open: bool
    last_success: datetime | None
    last_failure: datetime | None


@dataclass(frozen=True)
class RecoveryAction:
    """Domain event: recovery action to be executed."""
    component: str
    action_type: str
    detail: str
    severity: int


@dataclass(frozen=True)
class Incident:
    """Domain model for watchdog incident."""
    id: int | None
    component: str
    severity: str
    detail: str
    created_at: datetime
    resolved_at: datetime | None = None
    resolution_note: str | None = None
