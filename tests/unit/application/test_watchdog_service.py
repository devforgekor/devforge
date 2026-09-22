#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/application/
"""Tests for WatchdogService (E2) — fake ports, no I/O."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from devforge.application.watchdog_service import WatchdogService
from devforge.core.config import WatchdogConfig
from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.types import HealthCheck, RecoveryAction


class FakePort:
    def __init__(self, checks: list[HealthCheck]) -> None:
        self._checks = checks

    async def check_health(self) -> list[HealthCheck]:
        return list(self._checks)


class FakeRecoveryPort:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok
        self.actions: list[RecoveryAction] = []

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        self.actions.append(action)
        return self._ok


class FakeIncidentRepo:
    def __init__(self) -> None:
        self.detects: list[tuple[str, str, str]] = []
        self.actions: list[tuple[int | None, str, bool]] = []
        self.resolved: list[str] = []
        self._next_id = 1

    async def record_detect(self, component: str, event_type: str, detail: str,
                            unit: str | None = None) -> int | None:
        self.detects.append((component, event_type, detail))
        inc_id = self._next_id
        self._next_id += 1
        return inc_id

    async def record_action(self, incident_id: int | None, action: str, ok: bool) -> None:
        self.actions.append((incident_id, action, ok))

    async def resolve_if_open(self, component: str) -> None:
        self.resolved.append(component)

    async def find_open(self, component: str | None = None) -> list:
        return []


class FakeNotifier:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str, str]] = []
        self.recoveries: list[tuple[str, str]] = []

    async def send_alert(self, component: str, state: str, detail: str) -> bool:
        self.alerts.append((component, state, detail))
        return True

    async def send_recovery(self, component: str, detail: str) -> bool:
        self.recoveries.append((component, detail))
        return True

    async def sd_notify(self, state: str) -> bool:
        return True


class FakeState:
    def __init__(self) -> None:
        self.saved: list[tuple[dict[str, Any], float, str]] = []

    def save(self, trackers: Any, last_heartbeat_ts: float, mode: str) -> bool:
        self.saved.append((dict(trackers), last_heartbeat_ts, mode))
        return True

    def load(self) -> dict[str, Any]:
        return {}


def _service(checks: list[HealthCheck], recovery: FakeRecoveryPort | None = None,
             notifier: FakeNotifier | None = None, incidents: FakeIncidentRepo | None = None,
             state: FakeState | None = None,
             registry: TrackerRegistry | None = None) -> WatchdogService:
    registry = registry or TrackerRegistry()
    coord = CheckCoordinator(registry, {"grp": FakePort(checks)})
    notifier = notifier or FakeNotifier()
    incidents = incidents or FakeIncidentRepo()
    return WatchdogService(
        config=WatchdogConfig(),
        registry=registry,
        check_coordinator=coord,
        recovery_coordinator=RecoveryCoordinator(registry),
        recovery_port=recovery or FakeRecoveryPort(),
        notification_ports=[notifier],
        incident_repo=incidents,  # type: ignore[arg-type]
        state_storage=state,
    )


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setattr("asyncio.sleep", AsyncMock())


@pytest.mark.asyncio
async def test_all_healthy() -> None:
    incidents = FakeIncidentRepo()
    notifier = FakeNotifier()
    svc = _service([HealthCheck("svc:x", True, "ok")], notifier=notifier, incidents=incidents)
    result = await svc.run_cycle()
    assert result["checks"] == 1 and result["failed"] == 0
    assert incidents.resolved == ["svc:x"]
    assert notifier.alerts == []


@pytest.mark.asyncio
async def test_single_failure_recovers() -> None:
    recovery = FakeRecoveryPort(ok=True)
    notifier = FakeNotifier()
    incidents = FakeIncidentRepo()
    svc = _service([HealthCheck("svc:x", False, "inactive")],
                   recovery=recovery, notifier=notifier, incidents=incidents)
    result = await svc.run_cycle()
    assert result["failed"] == 1
    assert len(recovery.actions) == 1 and recovery.actions[0].kind == "service"
    assert incidents.detects and incidents.actions == [(1, "service", True)]
    assert notifier.recoveries
    # recovery succeeded → tracker reset to HEALTHY → no alert
    assert notifier.alerts == []


@pytest.mark.asyncio
async def test_alert_only_when_degraded_and_dedup() -> None:
    notifier = FakeNotifier()
    # alert-only component (no recovery kind) → state stays DEGRADED → alert
    svc = _service([HealthCheck("syssvc:caddy", False, "inactive")],
                   recovery=FakeRecoveryPort(), notifier=notifier)
    await svc.run_cycle()
    await svc.run_cycle()
    assert len(notifier.alerts) == 1  # second cycle deduped


@pytest.mark.asyncio
async def test_incident_resolved_on_healthy() -> None:
    incidents = FakeIncidentRepo()
    svc = _service([HealthCheck("svc:x", True, "ok")], incidents=incidents)
    await svc.run_cycle()
    assert incidents.resolved == ["svc:x"]


@pytest.mark.asyncio
async def test_persistence_save_called() -> None:
    state = FakeState()
    svc = _service([HealthCheck("svc:x", True, "ok")], state=state)
    await svc.run_cycle()
    assert len(state.saved) == 1
    _, ts, mode = state.saved[0]
    assert ts == 0.0 and mode == "day"


@pytest.mark.asyncio
async def test_circuit_open_skips_recovery() -> None:
    registry = TrackerRegistry()
    for _ in range(3):
        registry.get("svc:x").record_failure()
    recovery = FakeRecoveryPort()
    svc = _service([HealthCheck("svc:x", False, "inactive")],
                   recovery=recovery, registry=registry)
    await svc.run_cycle()
    assert recovery.actions == []


@pytest.mark.asyncio
async def test_failed_recovery_does_not_double_count() -> None:
    """Legacy parity: a component outcome records exactly once per cycle.

    The CheckCoordinator records the failure; a FAILED recovery must not
    record again (v2.1 guide E2 double-counted).
    """
    recovery = FakeRecoveryPort(ok=False)
    svc = _service([HealthCheck("svc:x", False, "inactive")], recovery=recovery)
    await svc.run_cycle()
    assert len(recovery.actions) == 1
    assert svc._registry.get("svc:x").consecutive_fail == 1


@pytest.mark.asyncio
async def test_backoff_defers_next_attempt_non_blocking() -> None:
    recovery = FakeRecoveryPort(ok=False)
    svc = _service([HealthCheck("svc:x", False, "inactive")], recovery=recovery)
    await svc.run_cycle()
    assert len(recovery.actions) == 1
    assert svc._registry.get("svc:x").can_attempt_recovery() is False
    # an immediate second cycle is deferred by the backoff gate
    await svc.run_cycle()
    assert len(recovery.actions) == 1


@pytest.mark.asyncio
async def test_component_states_and_resolve_incident() -> None:
    incidents = FakeIncidentRepo()
    svc = _service([HealthCheck("svc:x", False, "inactive")],
                   recovery=FakeRecoveryPort(ok=False), incidents=incidents)
    await svc.run_cycle()
    states = svc.component_states()
    assert states and states[0]["name"] == "svc:x" and states[0]["state"] == "DEGRADED"
    await svc.resolve_incident(1, "note")
    assert (1, "manual: note", True) in incidents.actions
