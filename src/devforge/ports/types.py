#!/usr/bin/env python3
# Status: experimental
# Path: ports/*, domain/watchdog/*, adapters/driven/*, application/*
"""Shared value objects for the watchdog subsystem.

Lives in ports/ (not domain/) so domain, adapters, and application can all
import it without layer inversion (import-linter: ports is the lowest layer).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class ComponentState(Enum):
    """Legacy state machine (state.py:21-25)."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"    # 1-2 consecutive failures, still retrying
    UNHEALTHY = "UNHEALTHY"  # 3+ consecutive failures, circuit OPEN
    DOWN = "DOWN"            # 5+ consecutive failures, escalated


@dataclass(frozen=True)
class HealthCheck:
    """Result of one component check (per-component, not aggregated)."""
    component: str          # namespaced tracker key, e.g. "svc:devforge-fastapi"
    is_healthy: bool
    detail: str
    metric_value: Optional[float] = None   # e.g. memory %, latency ms
    threshold: Optional[float] = None


@dataclass(frozen=True)
class RecoveryAction:
    """A recovery to be executed by a driven adapter.

    `kind` selects the adapter command and mirrors the legacy recover_* family
    (recovery.py) — there is no soft/medium/hard enum in production.
    """
    component: str          # namespaced tracker key
    kind: str               # "service" | "container" | "svcpod" | "oneshot" | "cascade" | "pipeline"
    reason: str
    backoff_sec: int = 0


@dataclass
class CircuitState:
    """Circuit breaker snapshot (state.py:134-141)."""
    is_open: bool
    failure_count: int
    opens_at: Optional[float] = None   # monotonic timestamp
    can_retry: bool = True


@dataclass(frozen=True)
class Incident:
    """Mirror of the production watchdog_incidents table (incidents.py:41-60)."""
    id: Optional[int]
    dedup_key: str
    component: str
    status: str
    symptom: Optional[str]
    context: Optional[str]
    detected_at: datetime
    last_seen_at: datetime
    action: Optional[str] = None
    action_result: Optional[str] = None
    action_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    fail_count: int = 1
    reopen_count: int = 0
