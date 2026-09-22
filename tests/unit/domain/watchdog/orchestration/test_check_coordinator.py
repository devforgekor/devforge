#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/orchestration/
"""Tests for CheckCoordinator (B3)."""
from __future__ import annotations

import pytest

from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator
from devforge.ports.types import HealthCheck


class FakePort:
    def __init__(self, checks: list[HealthCheck]) -> None:
        self._checks = checks

    async def check_health(self) -> list[HealthCheck]:
        return list(self._checks)


@pytest.mark.asyncio
async def test_run_records_all() -> None:
    reg = TrackerRegistry()
    coord = CheckCoordinator(reg, {"svc": FakePort([
        HealthCheck(component="svc:a", is_healthy=True, detail="ok"),
        HealthCheck(component="svc:b", is_healthy=False, detail="down"),
    ])})
    checks = await coord.run()
    assert len(checks) == 2
    assert reg.get("svc:a").state.value == "HEALTHY"
    assert reg.get("svc:b").state.value == "DEGRADED"


@pytest.mark.asyncio
async def test_run_subset_groups() -> None:
    coord = CheckCoordinator(TrackerRegistry(), {
        "a": FakePort([HealthCheck(component="svc:a", is_healthy=True, detail="")]),
        "b": FakePort([HealthCheck(component="svc:b", is_healthy=True, detail="")]),
    })
    checks = await coord.run(groups=["a"])
    assert [c.component for c in checks] == ["svc:a"]


@pytest.mark.asyncio
async def test_unknown_group_skipped() -> None:
    coord = CheckCoordinator(TrackerRegistry(), {})
    assert await coord.run(groups=["missing"]) == []


def test_failed_filter() -> None:
    checks = [HealthCheck("svc:a", True, ""), HealthCheck("svc:b", False, "")]
    assert CheckCoordinator.failed(checks) == ["svc:b"]


def test_healthy_filter() -> None:
    checks = [HealthCheck("svc:a", True, ""), HealthCheck("svc:b", False, "")]
    assert CheckCoordinator.healthy(checks) == ["svc:a"]


@pytest.mark.asyncio
async def test_empty_port_list() -> None:
    coord = CheckCoordinator(TrackerRegistry(), {"svc": FakePort([])})
    assert await coord.run() == []
