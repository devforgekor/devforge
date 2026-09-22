#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for domain/watchdog model and ports/types."""
from __future__ import annotations

from datetime import datetime, timezone

from devforge.ports.types import (
    ComponentState,
    ComponentStatus,
    HealthCheckResult,
    Incident,
    RecoveryAction,
)


class TestComponentState:
    def test_values(self) -> None:
        assert ComponentState.HEALTHY.value == "healthy"
        assert ComponentState.DEGRADED.value == "degraded"
        assert ComponentState.CRITICAL.value == "critical"
        assert ComponentState.RECOVERING.value == "recovering"


class TestHealthCheckResult:
    def test_frozen(self) -> None:
        r = HealthCheckResult(component="c", ok=True, detail="ok")
        assert r.component == "c"
        assert r.ok is True
        assert r.timestamp is not None

    def test_equality(self) -> None:
        t = datetime.now(timezone.utc)
        r1 = HealthCheckResult(component="c", ok=True, detail="ok", timestamp=t)
        r2 = HealthCheckResult(component="c", ok=True, detail="ok", timestamp=t)
        assert r1 == r2


class TestComponentStatus:
    def test_frozen(self) -> None:
        s = ComponentStatus(
            name="c",
            state=ComponentState.HEALTHY,
            fail_count=0,
            consecutive_failures=0,
            circuit_open=False,
            last_success=datetime.now(timezone.utc),
            last_failure=None,
        )
        assert s.state == ComponentState.HEALTHY
        assert s.circuit_open is False


class TestRecoveryAction:
    def test_frozen(self) -> None:
        a = RecoveryAction(
            component="c", action_type="restart", detail="test", severity=1
        )
        assert a.component == "c"
        assert a.severity == 1


class TestIncident:
    def test_frozen(self) -> None:
        i = Incident(
            id=None,
            component="c",
            severity="critical",
            detail="test",
            created_at=datetime.now(timezone.utc),
        )
        assert i.id is None
        assert i.resolved_at is None

    def test_with_resolution(self) -> None:
        i = Incident(
            id=1,
            component="c",
            severity="critical",
            detail="test",
            created_at=datetime.now(timezone.utc),
            resolved_at=datetime.now(timezone.utc),
            resolution_note="fixed",
        )
        assert i.resolution_note == "fixed"
