#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_watchdog_orchestrator_services.py — legacy watchdog _run_services
"""Regression: pause 플래그가 critical-service 복구를 막아 의도적 정지를 유지한다.

배경(2026-09-23): day-cycle은 SERVICE_TARGETS(critical)에 포함되어, 사용자가
`systemctl --user stop`해도 watchdog 복구가 재기동했다. pause 플래그를 복구
경로에서도 존중하도록 수정.
"""
from __future__ import annotations

import pytest
from lib.watchdog import orchestrator as orch


class FakeTracker:
    def __init__(self) -> None:
        self.success = 0
        self.failures = 0

    def record_success(self) -> None:
        self.success += 1

    def record_failure(self) -> bool:
        self.failures += 1
        return False

    def is_degraded(self) -> bool:
        return False

    def can_alert(self) -> bool:
        return False


class FakeState:
    def get(self, key: str) -> FakeTracker:
        return FakeTracker()


@pytest.fixture
def harness(monkeypatch):
    calls = {"recover": 0}

    monkeypatch.setattr(orch, "_state", FakeState())
    monkeypatch.setattr(orch, "_test_active", False)
    monkeypatch.setattr(orch, "is_experiment_active", lambda: False)
    monkeypatch.setattr(
        orch,
        "check_all_services",
        lambda: [{"name": "devforge-day-cycle", "ok": False, "detail": "inactive"}],
    )

    def _recover(name, tracker, fn):
        calls["recover"] += 1
        return True

    monkeypatch.setattr(orch, "graduated_recover", _recover)
    monkeypatch.setattr(orch.incidents, "record_detect", lambda *a, **k: 1)
    monkeypatch.setattr(orch.incidents, "record_action", lambda *a, **k: None)
    monkeypatch.setattr(orch.incidents, "resolve_if_open", lambda *a, **k: None)
    return calls


def test_should_skip_recovery_when_day_cycle_paused(harness, monkeypatch):
    monkeypatch.setattr(orch, "_day_cycle_paused", lambda: True)
    results = {"services": []}

    orch._run_services(results, dry_run=False)

    assert harness["recover"] == 0
    assert len(results["services"]) == 1


def test_should_recover_when_day_cycle_not_paused(harness, monkeypatch):
    monkeypatch.setattr(orch, "_day_cycle_paused", lambda: False)
    results = {"services": []}

    orch._run_services(results, dry_run=False)

    assert harness["recover"] == 1
