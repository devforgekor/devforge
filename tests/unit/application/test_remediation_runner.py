#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/application/
"""Tests for RemediationRunner — step-by-step plan execution."""

from __future__ import annotations

from typing import Any

import pytest

from devforge.application.remediation_runner import RemediationRunner
from devforge.ports.types import RemediationPlan, RemediationStep


class FakeHealth:
    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy

    async def check_health(self, component: str) -> Any:
        from devforge.ports.types import HealthCheck

        return HealthCheck(component=component, is_healthy=self._healthy, detail="")


class FakeRecovery:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute_recovery(self, component: str, params: dict[str, Any]) -> bool:
        self.calls.append((component, params))
        return self._ok


class FakeIncidents:
    def __init__(self) -> None:
        self.actions: list[tuple[Any, str, bool]] = []

    async def record_action(self, incident_id: Any, action: str, ok: bool, error: Any = None) -> None:
        self.actions.append((incident_id, action, ok))


def _plan() -> RemediationPlan:
    return RemediationPlan(
        component="svc:test",
        steps=[
            RemediationStep("pre-check", "pre-check", {}),
            RemediationStep("execute", "execute", {"action": "restart"}),
            RemediationStep("verify", "verify", {}),
            RemediationStep("record", "record", {"inc_id": 42, "action": "test"}),
        ],
    )


@pytest.mark.asyncio
async def test_should_execute_plan_steps_in_order() -> None:
    health = FakeHealth(healthy=True)
    recovery = FakeRecovery(ok=True)
    incidents = FakeIncidents()
    runner = RemediationRunner(recovery, health, incidents)
    result = await runner.execute(_plan())
    assert result.success
    assert len(result.steps) == 4
    assert all(s.success for s in result.steps)


@pytest.mark.asyncio
async def test_should_fail_when_step_fails() -> None:
    health = FakeHealth(healthy=False)
    recovery = FakeRecovery(ok=True)
    incidents = FakeIncidents()
    runner = RemediationRunner(recovery, health, incidents)
    result = await runner.execute(_plan())
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_should_record_action_on_success() -> None:
    health = FakeHealth(healthy=True)
    recovery = FakeRecovery(ok=True)
    incidents = FakeIncidents()
    runner = RemediationRunner(recovery, health, incidents)
    await runner.execute(_plan(), inc_id=42)
    assert len(incidents.actions) > 0
