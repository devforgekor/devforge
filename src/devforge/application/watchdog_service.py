#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, adapters/driving/cli_cmds/watchdog.py, systemd
"""Watchdog application service — orchestrates health monitoring, recovery, notification."""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from devforge.core.config import WatchdogConfig
from devforge.domain.watchdog.model import ComponentState, Incident
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator, CheckPlan
from devforge.domain.watchdog.orchestration.fix_coordinator import FixCoordinator
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.notification import NotificationPort
from devforge.ports.recovery import RecoveryPort


class WatchdogService:
    """Application service for watchdog orchestration.

    Coordinates health monitoring, recovery, and notification.
    """

    def __init__(
        self,
        config: WatchdogConfig,
        health_ports: Mapping[str, HealthCheckPort],
        recovery_ports: Mapping[str, RecoveryPort],
        notification_ports: Sequence[NotificationPort],
        incident_repo: IncidentRepository,
    ) -> None:
        # Core domain
        self._tracker = ComponentTracker(
            failure_threshold=config.failure_threshold,
            success_threshold=config.success_threshold,
        )
        self._recovery_coordinator = RecoveryCoordinator(self._tracker)

        # Orchestration
        self._check_coordinator = CheckCoordinator(self._tracker, health_ports)
        self._fix_coordinator = FixCoordinator(
            self._recovery_coordinator,
            recovery_ports,
        )

        # Infrastructure
        self._notification_ports = notification_ports
        self._incident_repo = incident_repo

        # Config
        self._config = config

    async def run_check_cycle(self) -> dict[str, Any]:
        """Execute one complete check-fix cycle.

        Returns:
            Summary dict with check/fix results.
        """
        # 1. Execute health checks
        plan = CheckPlan(
            components=list(self._config.critical_services),
            parallel=True,
            timeout_per_check=self._config.check_timeout_sec,
        )

        check_results = await self._check_coordinator.execute_checks(plan)

        # 2. Identify failures
        failed = [r.component for r in check_results if not r.ok]

        # 3. Execute fixes
        fix_results = {}
        if failed:
            fix_results = await self._fix_coordinator.execute_fixes(failed)

        # 4. Create incidents for new failures
        for component in failed:
            status = self._tracker.get_status(component)
            if status and status.state == ComponentState.CRITICAL:
                # Check if already open incident
                open_incidents = await self._incident_repo.find_open(component)
                if not open_incidents:
                    incident = Incident(
                        id=None,
                        component=component,
                        severity=status.state.value,
                        detail=f"Consecutive failures: {status.consecutive_failures}",
                        created_at=datetime.now(timezone.utc),
                        resolved_at=None,
                        resolution_note=None,
                    )
                    await self._incident_repo.save(incident)

        # 5. Send notifications
        for component in failed:
            status = self._tracker.get_status(component)
            if status:
                for notifier in self._notification_ports:
                    await notifier.send_alert(component, status)

        # 6. Notify recovery results
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
        """Manually resolve an incident."""
        return await self._incident_repo.resolve(incident_id, note)

    def get_component_status(self, component: str) -> Any:
        """Get current status of component."""
        return self._tracker.get_status(component)

    def get_all_statuses(self) -> Any:
        """Get status of all tracked components."""
        return self._tracker.all_statuses()


async def create_watchdog_service(config: WatchdogConfig) -> WatchdogService:
    """Factory function to create fully-wired WatchdogService."""
    from devforge.adapters.driven.health.llm_health import LLMHealthCheck
    from devforge.adapters.driven.health.system_health import (
        DiskHealthCheck,
        MemoryHealthCheck,
    )
    from devforge.adapters.driven.health.systemd_health import (
        SystemdServiceHealthCheck,
        SystemdTimerHealthCheck,
    )
    from devforge.adapters.driven.notification.slack_notifier import SlackNotifier
    from devforge.adapters.driven.notification.systemd_notifier import SystemdNotifier
    from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
    from devforge.adapters.driven.storage.incident_pg import PostgreSQLIncidentRepository
    from devforge.core.config import get_config
    from devforge.core.database import DatabaseGateway

    # Health check ports
    health_ports: Mapping[str, HealthCheckPort] = {
        "systemd-services": SystemdServiceHealthCheck(config.critical_services),
        "systemd-timers": SystemdTimerHealthCheck([
            "devforge-day-cycle.timer",
            "devforge-night-cycle.timer",
        ]),
        "llm-pods": LLMHealthCheck(config.llm_targets),
        "memory": MemoryHealthCheck(threshold_percent=90.0),
        "disk-root": DiskHealthCheck("/", threshold_percent=90.0),
        "disk-data": DiskHealthCheck("/opt/ai_data", threshold_percent=90.0),
    }

    # Recovery ports
    recovery_ports: Mapping[str, RecoveryPort] = {
        svc: SystemdRecoveryAdapter()
        for svc in config.critical_services
    }

    # Notification ports
    app_config = get_config()
    notification_ports: Sequence[NotificationPort] = [
        SystemdNotifier(),
    ]
    if app_config.secrets.SLACK_BOT_TOKEN_KEY:
        # Use the webhook URL from secrets
        webhook_url = os.environ.get("DEVFORGE_SLACK_WEBHOOK")
        if webhook_url:
            notification_ports = list(notification_ports) + [SlackNotifier(webhook_url)]

    # Incident repository
    db = DatabaseGateway(app_config.db_url_async)
    incident_repo = PostgreSQLIncidentRepository(db.session_maker)

    return WatchdogService(
        config=config,
        health_ports=health_ports,
        recovery_ports=recovery_ports,
        notification_ports=notification_ports,
        incident_repo=incident_repo,
    )
