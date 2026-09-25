#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/application/
"""Tests for WatchdogService dry_run mode (Gate 4 code prereq)."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import pytest

from devforge.application.watchdog_service import WatchdogService
from devforge.core.config import WatchdogConfig
from devforge.ports.types import (
    ComponentState,
    HealthCheck,
    Incident,
    RecoveryAction,
    SloTarget,
)


@dataclass
class FakeTracker:
    name: str
    state: ComponentState = ComponentState.HEALTHY
    consecutive_fail: int = 0
    fail_count: int = 0
    circuit_open_until: float = 0.0
    last_alert_ts: float = 0.0
    next_attempt_at: float = 0.0

    def is_healthy(self) -> bool:
        return self.state == ComponentState.HEALTHY

    def is_degraded(self) -> bool:
        return self.state == ComponentState.DEGRADED

    def can_alert(self, dedup_sec: int = 300) -> bool:
        return (time.monotonic() - self.last_alert_ts) >= dedup_sec

    def can_attempt_recovery(self) -> bool:
        return time.monotonic() >= self.next_attempt_at

    def schedule_next_attempt(self, sec: int) -> None:
        self.next_attempt_at = time.monotonic() + sec

    def record_failure(self) -> bool:
        self.consecutive_fail += 1
        self.fail_count += 1
        if self.consecutive_fail >= 5:
            self.state = ComponentState.DOWN
        elif self.consecutive_fail >= 3:
            self.state = ComponentState.UNHEALTHY
        elif self.consecutive_fail >= 1:
            self.state = ComponentState.DEGRADED
        return True

    def record_success(self) -> None:
        self.consecutive_fail = 0
        self.state = ComponentState.HEALTHY

    def record_check(self, check: HealthCheck) -> bool:
        if check.is_healthy:
            self.record_success()
            return False
        else:
            return self.record_failure()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_fail": self.consecutive_fail,
            "fail_count": self.fail_count,
            "circuit_open_until": self.circuit_open_until,
            "last_alert_ts": self.last_alert_ts,
            "next_attempt_at": self.next_attempt_at,
        }


class FakeRegistry:
    def __init__(self) -> None:
        self._trackers: dict[str, FakeTracker] = {}

    def get(self, name: str) -> FakeTracker:
        if name not in self._trackers:
            self._trackers[name] = FakeTracker(name)
        return self._trackers[name]

    def all(self) -> Mapping[str, FakeTracker]:
        return self._trackers


class FakeCheckCoordinator:
    def __init__(self, checks: list[HealthCheck], registry: FakeRegistry | None = None) -> None:
        self._checks = checks
        self._registry = registry

    async def run(self) -> list[HealthCheck]:
        if self._registry:
            for check in self._checks:
                self._registry.get(check.component).record_check(check)
        return list(self._checks)

    @staticmethod
    def failed(checks: list[HealthCheck]) -> list[str]:
        return [c.component for c in checks if not c.is_healthy]


class FakeRecoveryCoordinator:
    def __init__(self, registry: FakeRegistry) -> None:
        self._registry = registry

    def plan(self, component: str, detail: str) -> Optional[RecoveryAction]:
        t = self._registry.get(component)
        if t.state == ComponentState.UNHEALTHY:
            return RecoveryAction(component=component, kind="service", reason=detail, backoff_sec=10)
        return None

    def record_result(self, component: str, ok: bool) -> None:
        t = self._registry.get(component)
        if ok:
            t.record_success()
        else:
            t.record_failure()

    def should_escalate(self, component: str) -> bool:
        return False


class FakeRecoveryPort:
    def __init__(self) -> None:
        self.actions: list[RecoveryAction] = []

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        self.actions.append(action)
        return True


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


class FakeIncidentRepo:
    def __init__(self) -> None:
        self.detect_calls: list[tuple[str, str, str]] = []
        self.action_calls: list[tuple[Optional[int], str, bool]] = []
        self.resolve_calls: list[str] = []
        self.incidents: list[Incident] = []

    async def record_detect(self, component: str, event_type: str, detail: str, unit: Optional[str] = None) -> Optional[int]:
        self.detect_calls.append((component, event_type, detail))
        return 1

    async def record_action(self, incident_id: Optional[int], action: str, ok: bool) -> None:
        self.action_calls.append((incident_id, action, ok))

    async def resolve_if_open(self, component: str) -> None:
        self.resolve_calls.append(component)

    async def find_open(self, component: Optional[str] = None) -> list:
        return []

    async def find_since(self, since) -> list:
        return list(self.incidents)


class FakeStateStorage:
    def __init__(self) -> None:
        self.saves: int = 0
        self.payload: Optional[dict] = None

    def save(self, trackers: Mapping[str, Any], last_heartbeat_ts: float, mode: str) -> bool:
        self.saves += 1
        self.payload = {"components": [t.to_dict() for t in trackers.values()], "last_heartbeat_ts": last_heartbeat_ts, "mode": mode}
        return True

    def load(self) -> dict:
        return {"components": [], "last_heartbeat_ts": 0.0, "mode": "day"}


@pytest.fixture
def service_components() -> dict:
    registry = FakeRegistry()
    return {
        "registry": registry,
        "check_coordinator": FakeCheckCoordinator([]),
        "recovery_coordinator": FakeRecoveryCoordinator(registry),
        "recovery_port": FakeRecoveryPort(),
        "notifier": FakeNotifier(),
        "incident_repo": FakeIncidentRepo(),
        "state_storage": FakeStateStorage(),
    }


class TestDryRunMode:
    """dry_run=True일 때 부수효과가 차단되는지 검증."""

    @pytest.mark.asyncio
    async def test_dry_run_does_not_write_incidents(self, service_components) -> None:
        comps = service_components
        checks = [HealthCheck(component="svc:a", is_healthy=False, detail="down")]
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )

        await svc.run_cycle()

        assert comps["incident_repo"].detect_calls == []
        assert comps["incident_repo"].action_calls == []
        assert comps["incident_repo"].resolve_calls == []

    @pytest.mark.asyncio
    async def test_dry_run_does_not_notify(self, service_components) -> None:
        comps = service_components
        checks = [HealthCheck(component="svc:a", is_healthy=False, detail="down")]
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )

        await svc.run_cycle()

        assert comps["notifier"].alerts == []
        assert comps["notifier"].recoveries == []

    @pytest.mark.asyncio
    async def test_dry_run_does_not_persist(self, service_components) -> None:
        comps = service_components
        checks = [HealthCheck(component="svc:a", is_healthy=False, detail="down")]
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )

        await svc.run_cycle()

        assert comps["state_storage"].saves == 0

    @pytest.mark.asyncio
    async def test_dry_run_does_not_execute_recovery(self, service_components) -> None:
        comps = service_components
        # UNHEALTHY 상태면 recovery action이 계획됨
        comps["registry"].get("svc:a").state = ComponentState.UNHEALTHY
        comps["registry"].get("svc:a").consecutive_fail = 3
        checks = [HealthCheck(component="svc:a", is_healthy=False, detail="down")]
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )

        await svc.run_cycle()

        assert comps["recovery_port"].actions == []

    @pytest.mark.asyncio
    async def test_dry_run_still_records_check(self, service_components) -> None:
        comps = service_components
        checks = [HealthCheck(component="svc:a", is_healthy=False, detail="down")]
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )

        await svc.run_cycle()

        # tracker가 DEGRADED로 업데이트되어야 함
        tracker = comps["registry"].get("svc:a")
        assert tracker.state == ComponentState.DEGRADED
        assert tracker.consecutive_fail == 1

    @pytest.mark.asyncio
    async def test_dry_run_flag_in_result(self, service_components) -> None:
        comps = service_components
        checks = [HealthCheck(component="svc:a", is_healthy=True, detail="ok")]
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )

        result = await svc.run_cycle()

        assert result["dry_run"] is True
        assert result["checks"] == 1
        assert result["failed"] == 0


class TestWatchdogServiceProperties:
    def test_dry_run_property(self, service_components) -> None:
        comps = service_components
        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=True,
        )
        assert svc.dry_run is True

        svc2 = WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=False,
        )
        assert svc2.dry_run is False

    def test_check_interval_sec_property(self, service_components) -> None:
        comps = service_components
        svc = WatchdogService(
            config=WatchdogConfig(check_interval_sec=42),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
        )
        assert svc.check_interval_sec == 42


class TestIncidentRecordPolicy:
    """Q2 record-policy: systemd 계열만 incident, 나머지는 복구/알림만."""

    @staticmethod
    def _service(comps, dry_run: bool = False) -> WatchdogService:
        return WatchdogService(
            config=WatchdogConfig(check_interval_sec=60),
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
            dry_run=dry_run,
        )

    @staticmethod
    def _with_checks(comps, checks: list[HealthCheck]) -> None:
        comps["check_coordinator"] = FakeCheckCoordinator(checks, comps["registry"])

    @pytest.mark.asyncio
    async def test_systemd_components_still_write_incidents(self, service_components) -> None:
        comps = service_components
        self._with_checks(
            comps,
            [
                HealthCheck(component="svc:a", is_healthy=False, detail="down"),
                HealthCheck(component="timer:t.timer", is_healthy=False, detail="delay"),
                HealthCheck(component="oneshot:o.service", is_healthy=False, detail="failed"),
                HealthCheck(component="syssvc:caddy", is_healthy=False, detail="down"),
            ],
        )
        await self._service(comps).run_cycle()
        assert [c for c, _, _ in comps["incident_repo"].detect_calls] == [
            "svc:a",
            "timer:t.timer",
            "oneshot:o.service",
            "syssvc:caddy",
        ]

    @pytest.mark.asyncio
    async def test_alert_only_component_skips_incident_but_still_recovers(
        self, service_components
    ) -> None:
        comps = service_components
        # consecutive_fail=3 → UNHEALTHY → recovery 계획됨
        tracker = comps["registry"].get("system:memory")
        tracker.consecutive_fail = 3
        tracker.state = ComponentState.UNHEALTHY
        self._with_checks(
            comps, [HealthCheck(component="system:memory", is_healthy=False, detail="swap=1520MB")]
        )
        await self._service(comps).run_cycle()

        assert comps["incident_repo"].detect_calls == []
        assert comps["incident_repo"].action_calls == []
        assert len(comps["recovery_port"].actions) == 1

    @pytest.mark.asyncio
    async def test_alert_only_component_still_notifies(self, service_components) -> None:
        comps = service_components
        self._with_checks(
            comps, [HealthCheck(component="llm:day-extract", is_healthy=False, detail="HTTP 503")]
        )
        await self._service(comps).run_cycle()

        assert comps["incident_repo"].detect_calls == []
        assert [a[0] for a in comps["notifier"].alerts] == ["llm:day-extract"]

    @pytest.mark.asyncio
    async def test_writes_incident_predicate(self, service_components) -> None:
        from devforge.application.watchdog_service import writes_incident

        assert writes_incident("svc:a")
        assert writes_incident("timer:t.timer")
        assert not writes_incident("system:memory")
        assert not writes_incident("llm:day-extract")
        assert not writes_incident("heartbeat:db")


class TestSloReport:
    def _service(self, comps, config: WatchdogConfig) -> WatchdogService:
        return WatchdogService(
            config=config,
            registry=comps["registry"],
            check_coordinator=comps["check_coordinator"],
            recovery_coordinator=comps["recovery_coordinator"],
            recovery_port=comps["recovery_port"],
            notification_ports=[comps["notifier"]],
            incident_repo=comps["incident_repo"],
            state_storage=comps["state_storage"],
        )

    @pytest.mark.asyncio
    async def test_no_targets_returns_empty(self, service_components) -> None:
        svc = self._service(service_components, WatchdogConfig(slo_targets=[]))
        assert await svc.slo_report() == []

    @pytest.mark.asyncio
    async def test_reports_breach_from_open_incident(self, service_components) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        service_components["incident_repo"].incidents = [
            Incident(
                id=1,
                dedup_key="svc:a:down",
                component="svc:a",
                status="open",
                symptom="down",
                context=None,
                detected_at=now - timedelta(hours=10),
                last_seen_at=now - timedelta(hours=10),
                resolved_at=None,
            )
        ]
        config = WatchdogConfig(slo_targets=[SloTarget("a", "svc:a", 0.99, 30)])
        svc = self._service(service_components, config)
        rows = await svc.slo_report(now=now)
        assert len(rows) == 1
        assert rows[0]["name"] == "a"
        assert rows[0]["breached"] is True
        assert abs(rows[0]["downtime_sec"] - 36000) < 1
