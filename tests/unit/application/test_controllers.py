#!/usr/bin/env python3.12
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
    ctrl = CatchupController(port, inc, canary_stage=2)
    outcome = await ctrl.reconcile("oneshot:devforge-backup.service", "failed", inc_id=7)
    assert outcome == "ran"
    assert port.calls == [("oneshot", "devforge-backup.service")]
    assert inc.actions == [(7, "catchup:oneshot_run", True)]


@pytest.mark.asyncio
async def test_should_kick_stale_timer() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), canary_stage=2)
    outcome = await ctrl.reconcile("timer:devforge-system-sync.timer", "delay")
    assert outcome == "ran"
    assert port.calls == [("timer", "devforge-system-sync.timer")]


@pytest.mark.asyncio
async def test_should_dedup_within_window() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), canary_stage=2, window_sec=600)
    assert await ctrl.reconcile("timer:x.timer", "delay") == "ran"
    assert await ctrl.reconcile("timer:x.timer", "delay") == "already-ran"
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_should_skip_non_catchup_components() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), canary_stage=2)
    assert await ctrl.reconcile("svc:devforge-day-cycle", "down") == "skip"
    assert port.calls == []


@pytest.mark.asyncio
async def test_should_report_retry_when_port_fails() -> None:
    ctrl = CatchupController(FakePort(ok=False), FakeIncidents(), canary_stage=2)
    assert await ctrl.reconcile("oneshot:devforge-backup.service", "failed") == "retry"


@pytest.mark.asyncio
async def test_should_escalate_and_not_run_at_max_attempts() -> None:
    port, inc = FakePort(), FakeIncidents()
    ctrl = CatchupController(port, inc, max_attempts=3, canary_stage=2)
    outcome = await ctrl.reconcile("oneshot:x.service", "failed", inc_id=9, fail_count=3)
    assert outcome == "escalate"
    assert port.calls == []
    assert inc.actions == [(9, "catchup:escalate", False)]


@pytest.mark.asyncio
async def test_should_skip_approval_for_non_mutating() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), approval_port=FakeApproval(approved=False))
    outcome = await ctrl.reconcile("syssvc:caddy", "down")
    assert outcome == "skip"
    assert port.calls == []


@pytest.mark.asyncio
async def test_should_block_non_canary_at_stage_0() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), canary_stage=0)
    outcome = await ctrl.reconcile("oneshot:devforge-backup.service", "failed")
    assert outcome == "canary-blocked"
    assert port.calls == []


@pytest.mark.asyncio
async def test_should_allow_canary_path_at_stage_0() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), canary_stage=0)
    outcome = await ctrl.reconcile("timer:devforge-system-sync.timer", "delay")
    assert outcome == "ran"
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_should_allow_expanded_path_at_stage_1() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), canary_stage=1)
    outcome = await ctrl.reconcile("oneshot:devforge-backup.service", "failed")
    assert outcome == "ran"
    assert len(port.calls) == 1


@pytest.mark.asyncio
async def test_should_skip_stale_incidents() -> None:
    port = FakePort()
    ctrl = CatchupController(port, FakeIncidents(), staleness_sec=3600, canary_stage=2)
    stale_ts = "2020-01-01T00:00:00Z"
    outcome = await ctrl.reconcile("oneshot:x.service", "failed", last_seen_at=stale_ts)
    assert outcome == "skip"
    assert port.calls == []


class FakeApproval:
    def __init__(self, approved: bool = True) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self._approved = approved

    async def request(self, req) -> None:
        self.requests.append((req.action_id, req.component, req.action))
        from devforge.ports.approval import ApprovalDecision

        return ApprovalDecision(req.action_id, self._approved, "tester", "")


@pytest.mark.asyncio
async def test_should_request_approval_before_running() -> None:
    port, inc = FakePort(), FakeIncidents()
    approval = FakeApproval(approved=True)
    ctrl = CatchupController(port, inc, approval_port=approval, canary_stage=2)
    outcome = await ctrl.reconcile("oneshot:devforge-backup.service", "failed", inc_id=10)
    assert outcome == "ran"
    assert len(approval.requests) == 1
    assert approval.requests[0][1] == "oneshot:devforge-backup.service"


@pytest.mark.asyncio
async def test_should_deny_and_not_run_when_approval_rejected() -> None:
    port, inc = FakePort(), FakeIncidents()
    approval = FakeApproval(approved=False)
    ctrl = CatchupController(port, inc, approval_port=approval, canary_stage=2)
    outcome = await ctrl.reconcile("oneshot:devforge-backup.service", "failed", inc_id=11)
    assert outcome == "denied"
    assert port.calls == []
    assert inc.actions == [(11, "catchup:denied", False)]
