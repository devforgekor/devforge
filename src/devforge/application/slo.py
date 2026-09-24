#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, adapters/driving/cli_cmds/watchdog.py
"""Availability SLI/SLO + error-budget derivation (pure, no I/O).

[WHY] There is no metrics backend yet (2026-standard-gap §10): until
Prometheus/OTel is available, availability is approximated locally from
watchdog incident downtime intervals. An incident's [detected_at, resolved_at]
span is treated as component unavailability; overlapping spans are unioned so a
single outage is never double-counted. Multi-window burn-rate alerting is a
follow-up once a real metrics backend exists; `burn_rate()` here is the
single-window error-budget consumption ratio over incident data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from devforge.ports.types import Incident, SloTarget


@dataclass(frozen=True)
class SloResult:
    name: str
    component: str
    objective: float
    window_days: int
    window_sec: float
    availability: float
    downtime_sec: float
    allowed_downtime_sec: float
    error_budget_remaining_sec: float
    burn_rate: float
    incident_count: int
    breached: bool


def _ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _overlap_sec(
    incidents: Sequence[Incident], component: str, start: datetime, end: datetime
) -> tuple[float, int]:
    """Union downtime of `component` incidents clipped to [start, end), plus count."""
    intervals: list[tuple[float, float]] = []
    count = 0
    for inc in incidents:
        if inc.component != component:
            continue
        inc_start = max(_ensure_utc(inc.detected_at), start)
        inc_end = _ensure_utc(inc.resolved_at) if inc.resolved_at is not None else end
        inc_end = min(inc_end, end)
        if inc_end > inc_start:
            intervals.append((inc_start.timestamp(), inc_end.timestamp()))
            count += 1
    if not intervals:
        return 0.0, count
    intervals.sort()
    total = 0.0
    cur_start, cur_end = intervals[0]
    for i_start, i_end in intervals[1:]:
        if i_start <= cur_end:
            cur_end = max(cur_end, i_end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = i_start, i_end
    total += cur_end - cur_start
    return total, count


def _cutoff(now: datetime, window_days: int) -> datetime:
    return now - timedelta(days=window_days)


def compute_slo(
    incidents: Sequence[Incident],
    target: SloTarget,
    *,
    now: Optional[datetime] = None,
) -> SloResult:
    """Derive availability, error budget, and burn rate for one target."""
    now = _ensure_utc(now or datetime.now(timezone.utc))
    cutoff = _cutoff(now, target.window_days)
    window_sec = (now - cutoff).total_seconds()

    downtime, count = _overlap_sec(incidents, target.component, cutoff, now)
    availability = max(0.0, 1.0 - (downtime / window_sec)) if window_sec else 1.0
    allowed = (1.0 - target.objective) * window_sec
    burn = downtime / allowed if allowed > 0 else (float("inf") if downtime > 0 else 0.0)

    return SloResult(
        name=target.name,
        component=target.component,
        objective=target.objective,
        window_days=target.window_days,
        window_sec=window_sec,
        availability=availability,
        downtime_sec=downtime,
        allowed_downtime_sec=allowed,
        error_budget_remaining_sec=allowed - downtime,
        burn_rate=burn,
        incident_count=count,
        breached=availability < target.objective,
    )


def compute_slos(
    incidents: Sequence[Incident],
    targets: Sequence[SloTarget],
    *,
    now: Optional[datetime] = None,
) -> list[SloResult]:
    return [compute_slo(incidents, t, now=now) for t in targets]


def burn_rate(
    incidents: Sequence[Incident],
    target: SloTarget,
    *,
    window_days: int,
    now: Optional[datetime] = None,
) -> float:
    """Error-budget burn rate over an arbitrary trailing window (local approximation)."""
    now = _ensure_utc(now or datetime.now(timezone.utc))
    cutoff = _cutoff(now, window_days)
    window_sec = (now - cutoff).total_seconds()
    allowed = (1.0 - target.objective) * window_sec
    if allowed <= 0:
        return 0.0
    downtime, _ = _overlap_sec(incidents, target.component, cutoff, now)
    return downtime / allowed
