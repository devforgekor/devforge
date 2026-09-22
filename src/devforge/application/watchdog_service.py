#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, adapters/driving/cli_cmds/watchdog.py, systemd
"""Watchdog application service — orchestrates health monitoring, recovery, notification."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from devforge.domain.watchdog.model import ComponentState, Incident
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator, CheckPlan
from devforge.domain.watchdog.orchestration.fix_coordinator import FixCoordinator
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.notification import NotificationPort


class WatchdogService:
    """Application service for watchdog orchestration.

    Coordinates health monitoring, recovery, and notification.
    """

    def __init__(
        self,
        tracker: ComponentTracker,
        check_coordinator: CheckCoordinator,
        fix_coordinator: FixCoordinator,
        recovery_coordinator: RecoveryCoordinator,
        notification_ports: list[NotificationPort],
        incident_repo: IncidentRepository,
        critical_services: list[str],
    ) -> None:
        self._tracker = tracker
        self._check_coordinator = check_coordinator
        self._fix_coordinator = fix_coordinator
        self._recovery_coordinator = recovery_coordinator
        self._notification_ports = notification_ports
        self._incident_repo = incident_repo
        self._critical_services = critical_services

    async def run_check_cycle(self, check_timeout: int = 30) -> dict[str, Any]:
        plan = CheckPlan(
            components=self._critical_services,
            parallel=True,
            timeout_per_check=check_timeout,
        )
        check_results = await self._check_coordinator.execute_checks(plan)
        failed = [r.component for r in check_results if not r.ok]

        fix_results: dict[str, bool] = {}
        if failed:
            fix_results = await self._fix_coordinator.execute_fixes(failed)

        for component in failed:
            status = self._tracker.get_status(component)
            if status and status.state == ComponentState.CRITICAL:
                open_incidents = await self._incident_repo.find_open(component)
                if not open_incidents:
                    incident = Incident(
                        id=None,
                        component=component,
                        severity=status.state.value,
                        detail=f"Consecutive failures: {status.consecutive_failures}",
                        created_at=datetime.now(timezone.utc),
                    )
                    await self._incident_repo.save(incident)

        for component in failed:
            status = self._tracker.get_status(component)
            if status:
                for notifier in self._notification_ports:
                    await notifier.send_alert(component, status)

        for component, success in fix_results.items():
            action = self._recovery_coordinator.plan_recovery(component)
            if action:
                for notifier in self._notification_ports:
                    await notifier.send_recovery(component, action, success)

        return {
            "checks": len(check_results),
            "failed": len(failed),
            "fixed": sum(1 for v in fix_results.values() if v),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def resolve_incident(self, incident_id: int, note: str) -> bool:
        return await self._incident_repo.resolve(incident_id, note)

    def get_component_status(self, component: str) -> Any:
        return self._tracker.get_status(component)

    def get_all_statuses(self) -> Any:
        return self._tracker.all_statuses()
