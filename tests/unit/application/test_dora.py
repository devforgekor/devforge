#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/application/
"""Tests for the pure DORA calculator (2026 standard-gap §12/§18.3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from devforge.application.dora import (
    Deployment,
    compute_dora,
    deployments_from_incidents,
)
from devforge.ports.types import Incident

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def test_empty_is_zero() -> None:
    m = compute_dora([], now=NOW)
    assert m.deployment_count == 0
    assert m.deployment_frequency_per_day == 0.0
    assert m.change_failure_rate == 0.0
    assert m.change_lead_time_sec is None
    assert m.failed_deployment_recovery_sec is None


def test_basic_metrics() -> None:
    deploys = [
        Deployment(at=_days_ago(1)),
        Deployment(at=_days_ago(2)),
        Deployment(at=_days_ago(3), failed=True, recovery_at=_days_ago(2.5)),
    ]
    m = compute_dora(deploys, window_days=30, now=NOW)
    assert m.deployment_count == 3 and m.failure_count == 1
    assert abs(m.change_failure_rate - 1 / 3) < 1e-9
    assert abs(m.deployment_frequency_per_day - 3 / 30) < 1e-9
    # recovery = 0.5 day = 43200s
    assert m.failed_deployment_recovery_sec is not None
    assert abs(m.failed_deployment_recovery_sec - 43200) < 1


def test_lead_time_mean() -> None:
    deploys = [
        Deployment(at=_days_ago(1), commit_at=_days_ago(1.5)),  # 0.5d = 43200s
        Deployment(at=_days_ago(2), commit_at=_days_ago(2.25)),  # 0.25d = 21600s
    ]
    m = compute_dora(deploys, now=NOW)
    assert m.change_lead_time_sec is not None
    assert abs(m.change_lead_time_sec - (43200 + 21600) / 2) < 1


def test_window_filters_old() -> None:
    deploys = [Deployment(at=_days_ago(1)), Deployment(at=_days_ago(40))]
    m = compute_dora(deploys, window_days=30, now=NOW)
    assert m.deployment_count == 1


def test_deployments_from_incidents() -> None:
    inc = Incident(
        id=1,
        dedup_key="svc:x:down",
        component="svc:x",
        status="open",
        symptom="down",
        context=None,
        detected_at=_days_ago(2),
        last_seen_at=_days_ago(2),
        action_result="fail",
        resolved_at=_days_ago(1),
        reopen_count=0,
    )
    deploys = deployments_from_incidents([inc])
    assert len(deploys) == 1 and deploys[0].failed is True
    m = compute_dora(deploys, now=NOW)
    assert m.failure_count == 1
    assert m.failed_deployment_recovery_sec is not None
    assert abs(m.failed_deployment_recovery_sec - 86400) < 1
