#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driving/
"""Tests for watchdog CLI (E3)."""
from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from devforge.adapters.driving.cli_cmds import watchdog as wd


class _StubService:
    def __init__(self) -> None:
        self.ran = 0
        self.resolved: list[tuple[int, str]] = []

    async def run_cycle(self) -> dict[str, int]:
        self.ran += 1
        return {"checks": 3, "failed": 1}

    def component_states(self) -> list[dict[str, object]]:
        return [{"name": "svc:x", "state": "DEGRADED", "fail_count": 1, "circuit_open": False}]

    async def resolve_incident(self, incident_id: int, note: str) -> None:
        self.resolved.append((incident_id, note))


@pytest.fixture(autouse=True)
def _reset_factory():  # type: ignore[no-untyped-def]
    yield
    wd._factory = None


def test_status_requires_factory() -> None:
    wd._factory = None
    runner = CliRunner()
    result = runner.invoke(wd.app, ["status"])
    assert result.exit_code != 0


def test_status_prints_state() -> None:
    svc = _StubService()
    wd.init(lambda: _return(svc))

    async def _coro() -> Any:
        return svc
    wd._factory = _coro
    runner = CliRunner()
    result = runner.invoke(wd.app, ["status"])
    assert "svc:x" in result.stdout


def test_check_runs_cycle() -> None:
    svc = _StubService()

    async def _coro() -> Any:
        return svc
    wd._factory = _coro
    runner = CliRunner()
    result = runner.invoke(wd.app, ["check"])
    assert "checks=3" in result.stdout and "failed=1" in result.stdout


def test_resolve_calls_incident_action() -> None:
    svc = _StubService()

    async def _coro() -> Any:
        return svc
    wd._factory = _coro
    runner = CliRunner()
    result = runner.invoke(wd.app, ["resolve", "7", "manual note"])
    assert result.exit_code == 0 and "resolved=True" in result.stdout


async def _return(svc: Any) -> Any:
    return svc
