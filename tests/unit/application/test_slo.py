#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/application/
"""Tests for the pure availability SLO calculator (2026 standard-gap §10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from devforge.application.slo import burn_rate, compute_slo, compute_slos
from devforge.ports.types import Incident, SloTarget

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _secs_ago(n: float) -> datetime:
    return NOW - timedelta(seconds=n)


def _inc(
    component: str,
    start_sec: float,
    end_sec: Optional[float],
    *,
    resolved: bool = True,
) -> Incident:
    start = _secs_ago(start_sec)
    end = _secs_ago(end_sec) if end_sec is not None else None
    return Incident(
        id=1,
        dedup_key=f"{component}:down",
        component=component,
        status="resolved" if resolved else "open",
        symptom="down",
        context=None,
        detected_at=start,
        last_seen_at=start,
        resolved_at=end,
    )


def test_empty_is_full_availability() -> None:
    r = compute_slo([], SloTarget("x", "svc:x", 0.99, 30), now=NOW)
    assert r.availability == 1.0
    assert r.downtime_sec == 0.0
    assert r.burn_rate == 0.0
    assert r.breached is False
    assert r.incident_count == 0
    assert abs(r.error_budget_remaining_sec - r.allowed_downtime_sec) < 1e-6


def test_single_closed_incident_downtime() -> None:
    inc = _inc("svc:x", 40000, 10000)  # 30000s = 8h20m
    r = compute_slo([inc], SloTarget("x", "svc:x", 0.99, 30), now=NOW)
    assert abs(r.downtime_sec - 30000) < 1
    assert r.incident_count == 1
    assert abs(r.availability - (1 - 30000 / r.window_sec)) < 1e-9
    assert r.breached is True  # 30000s > 25920s budget


def test_open_incident_counts_from_detection_to_now() -> None:
    inc = _inc("svc:x", 3600, None, resolved=False)
    r = compute_slo([inc], SloTarget("x", "svc:x", 0.99, 30), now=NOW)
    assert abs(r.downtime_sec - 3600) < 1


def test_overlapping_incidents_are_unioned() -> None:
    # [now-5000, now-2000] ∪ [now-4000, now-1000] = 4000s (not 6000s)
    a = _inc("svc:x", 5000, 2000)
    b = _inc("svc:x", 4000, 1000)
    r = compute_slo([a, b], SloTarget("x", "svc:x", 0.99, 30), now=NOW)
    assert abs(r.downtime_sec - 4000) < 1
    assert r.incident_count == 2


def test_window_clips_incident_start() -> None:
    # 2-day incident, but only the last 12h fall inside a 1-day window
    inc = _inc("svc:x", 2 * 86400, 12 * 3600)
    r = compute_slo([inc], SloTarget("x", "svc:x", 0.99, 1), now=NOW)
    assert abs(r.downtime_sec - 43200) < 1


def test_non_matching_component_ignored() -> None:
    r = compute_slo([_inc("svc:y", 1000, 0)], SloTarget("x", "svc:x", 0.99, 30), now=NOW)
    assert r.downtime_sec == 0.0
    assert r.incident_count == 0


def test_incident_outside_window_excluded() -> None:
    inc = _inc("svc:x", 40 * 86400, 39 * 86400)
    r = compute_slo([inc], SloTarget("x", "svc:x", 0.99, 30), now=NOW)
    assert r.downtime_sec == 0.0
    assert r.incident_count == 0


def test_compute_slos_returns_one_per_target() -> None:
    targets = [SloTarget("x", "svc:x"), SloTarget("y", "svc:y")]
    results = compute_slos([_inc("svc:x", 100, 0)], targets, now=NOW)
    assert [r.name for r in results] == ["x", "y"]
    assert results[1].downtime_sec == 0.0


def test_burn_rate_over_custom_window() -> None:
    # 1-day window, 99% objective -> 864s budget; 432s down = 0.5 burn
    inc = _inc("svc:x", 10000, 9568)
    rate = burn_rate([inc], SloTarget("x", "svc:x", 0.99, 30), window_days=1, now=NOW)
    assert abs(rate - 0.5) < 1e-6


def test_perfect_objective_does_not_divide_by_zero() -> None:
    r = compute_slo([_inc("svc:x", 100, 0)], SloTarget("x", "svc:x", 1.0, 30), now=NOW)
    assert r.allowed_downtime_sec == 0.0
    assert r.burn_rate == float("inf")
    assert r.breached is True
