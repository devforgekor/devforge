#!/usr/bin/env python3
# Status: experimental
# Path: none — test only
"""PipelineOrchestrator + Budget unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from devforge.application.orchestrator import (
    Budget,
    PipelineBudgetError,
    PipelineOrchestrator,
)

_KST = timezone(timedelta(hours=9))


class FakeStage:
    def __init__(self, name: str, result: str = "ok") -> None:
        self.name = name
        self._result = result

    def run(self) -> str:
        return self._result


def test_budget_remaining_positive():
    b = Budget(limit_sec=100, started_at=datetime.now(tz=_KST))
    assert 95 < b.remaining() <= 100
    assert not b.expired()


def test_budget_expired():
    b = Budget(limit_sec=0, started_at=datetime.now(tz=_KST) - timedelta(seconds=10))
    assert b.expired()
    assert b.remaining() == 0.0


def test_budget_gate_raises():
    b = Budget(limit_sec=0, started_at=datetime.now(tz=_KST) - timedelta(seconds=10))
    with pytest.raises(PipelineBudgetError):
        b.gate()


def test_orchestrator_runs_all_stages():
    stages = [FakeStage("s1"), FakeStage("s2"), FakeStage("s3")]
    budget = Budget(limit_sec=3600)
    orch = PipelineOrchestrator(stages, budget)
    results = orch.run()
    assert results == ["ok", "ok", "ok"]


def test_orchestrator_stops_on_budget():
    stages = [FakeStage("s1"), FakeStage("s2")]
    budget = Budget(limit_sec=0, started_at=datetime.now(tz=_KST) - timedelta(seconds=10))
    orch = PipelineOrchestrator(stages, budget)
    with pytest.raises(PipelineBudgetError):
        orch.run()
