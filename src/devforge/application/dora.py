#!/usr/bin/env python3
# Status: experimental
# Path: none — pure DORA calculator (wiring to state.yaml.dora is a follow-up)
"""DORA metrics derivation (pure, no I/O).

Computes the four DORA keys (2026) from deployment/incident events:
  - deployment frequency        = deployments / window_days
  - change lead time            = mean(deploy.at - deploy.commit_at)
  - change failure rate         = failed deployments / deployments
  - failed deployment recovery  = mean(recovery_at - at) for failed deploys

Inputs are plain dataclasses so this stays testable and layer-clean
(application -> ports/domain). Wiring (watchdog_incidents + git -> state.yaml.dora)
is a separate follow-up; see docs/plans/2026-standard-gap-remediation.md §18.3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from devforge.ports.types import Incident

DEFAULT_WINDOW_DAYS = 30


@dataclass(frozen=True)
class Deployment:
    """A production deployment event."""

    at: datetime
    failed: bool = False
    recovery_at: Optional[datetime] = None  # set when failed and remediated
    commit_at: Optional[datetime] = None  # set when lead time is derivable


@dataclass(frozen=True)
class DoraMetrics:
    window_days: int
    deployment_count: int
    failure_count: int
    deployment_frequency_per_day: float
    change_lead_time_sec: Optional[float]
    change_failure_rate: float
    failed_deployment_recovery_sec: Optional[float]


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _mean(values: Sequence[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def compute_dora(
    deployments: Sequence[Deployment],
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> DoraMetrics:
    """Derive the four DORA keys over a trailing window."""
    now = _ensure_utc(now or datetime.now(timezone.utc))
    cutoff = now - timedelta(days=window_days)
    window = [d for d in deployments if _ensure_utc(d.at) >= cutoff]

    count = len(window)
    failures = [d for d in window if d.failed]
    recovery = [
        (_ensure_utc(d.recovery_at) - _ensure_utc(d.at)).total_seconds()
        for d in failures
        if d.recovery_at is not None
    ]
    lead = [
        (_ensure_utc(d.at) - _ensure_utc(d.commit_at)).total_seconds()
        for d in window
        if d.commit_at is not None
    ]

    return DoraMetrics(
        window_days=window_days,
        deployment_count=count,
        failure_count=len(failures),
        deployment_frequency_per_day=(count / window_days) if window_days else 0.0,
        change_lead_time_sec=_mean(lead),
        change_failure_rate=(len(failures) / count) if count else 0.0,
        failed_deployment_recovery_sec=_mean(recovery),
    )


def deployments_from_incidents(incidents: Sequence[Incident]) -> list[Deployment]:
    """Approximate deployments from watchdog incidents (CFR/recovery only).

    [WHY] There is no explicit deployment event store yet. An incident is treated
    as a *failed* change with recovery time = resolved_at - detected_at, so CFR
    and recovery time can be derived today. Deployment frequency and lead time
    need a real deploy/commit source (git/CI) and are intentionally left out.
    """
    out: list[Deployment] = []
    for inc in incidents:
        failed = inc.action_result == "fail" or inc.reopen_count > 0
        out.append(
            Deployment(
                at=inc.detected_at,
                failed=failed,
                recovery_at=inc.resolved_at,
            )
        )
    return out
