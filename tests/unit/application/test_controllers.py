#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/application/
"""Tests for the B catch-up controller (S3)."""
from __future__ import annotations

from typing import Any, Optional

import pytest

from devforge.application.controllers import CatchupController


class FakePort:
    def __init__(self, ok: bool = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._ok = ok

    async def run_oneshot(self, unit: str) -> bool:
        self.calls.append(("oneshot", unit))
        return self._ok

    async def kick_timer(self, timer: str) -> bool:
        self.calls.append(("timer", timer))
        return self._ok


class FakeIncidents:
    def __init__(self) -> None:
        self.actions: list[tuple[Optional[int], str, bool]] = []

    async def record_action(
        self, incident_id: Optional[int], action: str, ok: bool, error: Optional[dict[str, Any]] = None
    ) -> None:
        self.actions.append((incident_id, action, ok))


@pytest.mark.asyncio
async def test_should_run_missed_oneshot_and_record() -> None:
    port, inc = FakePort(), FakeIncidents()
    ctrl = CatchupController(port, inc)
    outcome = await ctrl.reconcile("oneshot:devforge-backup.service", "failed", inc_id=7)
    assert outcome == "ran"
    assert port.calls == [("oneshot", "devforge-backup.service")]
    assert inc.actions == [(7, "catchup:oneshot_run", True)]


@pytest.mark.asyncio
async def test_should_kick_stale_timer() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents())
    outcome = await ctrl.reconcile("timer:devforge-system-sync.timer", "delay")
    assert outcome == "ran"
    assert port.calls == [("timer", "devforge-system-sync.timer")]


@pytest.mark.asyncio
async def test_should_dedup_within_window() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), window_sec=600)
    assert await ctrl.reconcile("timer:x.timer", "delay") == "ran"
    assert await ctrl.reconcile("timer:x.timer", "delay") == "already-ran"
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_should_skip_non_catchup_components() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents())
    assert await ctrl.reconcile("svc:devforge-day-cycle", "down") == "skip"
    assert port.calls == []


@pytest.mark.asyncio
async def test_should_report_retry_when_port_fails() -> None:
    ctrl = CatchupController(FakePort(ok=False), FakeIncidents())
    assert await ctrl.reconcile("oneshot:devforge-backup.service", "failed") == "retry"
