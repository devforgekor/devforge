#!/usr/bin/env python3
# Status: experimental
# Path: application/
"""Watchdog application service (legacy orchestrator.py main loop)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Optional

from devforge.core.config import WatchdogConfig
from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.notification import NotificationPort
from devforge.ports.recovery import RecoveryPort
from devforge.ports.state_persistence import StateStoragePort

log = logging.getLogger(__name__)

_EVENT_TYPE = {
    "svc": "down",
    "timer": "delay",
    "oneshot": "failed",
    "syssvc": "down",
    "llm": "down",
    "pipeline": "stuck",
    "system": "crit",
    "infra": "down",
}


class WatchdogService:
    def __init__(
        self,
        config: WatchdogConfig,
        registry: TrackerRegistry,
        check_coordinator: CheckCoordinator,
        recovery_coordinator: RecoveryCoordinator,
        recovery_port: RecoveryPort,
        notification_ports: Sequence[NotificationPort],
        incident_repo: IncidentRepository,
        state_storage: Optional[StateStoragePort] = None,
        dry_run: bool = False,
    ) -> None:
        self._config = config
        self._registry = registry
        self._checks = check_coordinator
        self._recovery = recovery_coordinator
        self._recovery_port = recovery_port
        self._notifiers = list(notification_ports)
        self._incidents = incident_repo
        self._state = state_storage
        self._mode = "day"
        self._last_heartbeat_ts = 0.0
        self._dry_run = dry_run

    def _event_type(self, component: str) -> str:
        return _EVENT_TYPE.get(component.split(":", 1)[0], "down")

    async def run_cycle(self) -> dict[str, Any]:
        checks = await self._checks.run()
        failed = self._checks.failed(checks)

        # 1. healthy → resolve incidents (skipped in dry_run)
        for c in checks:
            if not c.is_healthy:
                continue
            if not self._dry_run:
                await self._incidents.resolve_if_open(c.component)
            if (
                c.metric_value is not None
                and c.threshold is not None
                and c.metric_value > c.threshold
            ) and not self._dry_run:
                for n in self._notifiers:
                    await n.send_alert(c.component, "LATENCY", c.detail)

        # 2. failed → incident → recovery → alert (orchestrator.py:128-143)
        #
        # Legacy parity note: a component outcome maps to EXACTLY ONE tracker
        # record call per cycle. The CheckCoordinator already recorded this
        # cycle's failure, so on a FAILED recovery we must not record again
        # (legacy's graduated_recover owns the single record call in the
        # recovery branch). Only a SUCCESSFUL recovery resets the tracker.
        for c in checks:
            if c.is_healthy:
                continue
            t = self._registry.get(c.component)

            if self._dry_run:
                action = self._recovery.plan(c.component, c.detail)
                log.info(
                    "[dry-run] %s failed: %s (would %s)",
                    c.component,
                    c.detail,
                    action.kind if action is not None else "alert-only",
                )
                continue

            inc_id = await self._incidents.record_detect(
                c.component, self._event_type(c.component), c.detail
            )
            action = self._recovery.plan(c.component, c.detail)
            if action is not None and t.can_attempt_recovery():
                # Non-blocking backoff: defer the next attempt instead of
                # sleeping the whole cycle (legacy sleeps in-line).
                t.schedule_next_attempt(action.backoff_sec)
                ok = await self._recovery_port.execute_recovery(action)
                await self._incidents.record_action(inc_id, action.kind, ok)
                if ok:
                    self._recovery.record_result(c.component, True)
                    for n in self._notifiers:
                        await n.send_recovery(c.component, f"{action.kind} ok")
            if t.is_degraded() and t.can_alert():
                for n in self._notifiers:
                    await n.send_alert(c.component, t.state.value, c.detail)

        if not self._dry_run:
            self._persist()
        return {
            "checks": len(checks),
            "failed": len(failed),
            "dry_run": self._dry_run,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def component_states(self) -> list[dict[str, object]]:
        """Read-model for the CLI: per-component summary (no private access)."""
        return [t.summary() for t in self._registry.all().values()]

    async def resolve_incident(self, incident_id: int, note: str) -> None:
        """Resolve an incident via the repository port."""
        await self._incidents.record_action(incident_id, f"manual: {note}", True)

    def _persist(self) -> None:
        if self._state is not None:
            self._state.save(self._registry.all(), self._last_heartbeat_ts, self._mode)

    def load_state(self) -> None:
        if self._state is not None:
            payload = self._state.load()
            self._registry.restore(
                {c["name"]: c for c in payload.get("components", []) if "name" in c}
            )
            self._last_heartbeat_ts = float(payload.get("last_heartbeat_ts", 0.0))
            self._mode = payload.get("mode", self._mode)

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def check_interval_sec(self) -> int:
        return self._config.check_interval_sec


def create_watchdog_service(config: WatchdogConfig, dry_run: bool = False) -> WatchdogService:
    """Composition factory — wires ports/adapters for the CLI (E3)."""
    from devforge.adapters.driven.health.ebook_health import EbookPipelineHealthChecker
    from devforge.adapters.driven.health.llm_health import LLMHealthChecker
    from devforge.adapters.driven.health.pipeline_health import HeartbeatHealthChecker
    from devforge.adapters.driven.health.svcpod_health import SvcpodForwardingHealthChecker
    from devforge.adapters.driven.health.system_health import DiskHealthChecker, MemoryHealthChecker
    from devforge.adapters.driven.health.systemd_health import (
        OneshotResultHealthChecker,
        SystemdServiceHealthChecker,
        SystemdSystemServiceHealthChecker,
        SystemdTimerHealthChecker,
    )
    from devforge.adapters.driven.notification.slack_notifier import SlackNotifier
    from devforge.adapters.driven.notification.systemd_notifier import SystemdNotifier
    from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.adapters.driven.storage.heartbeat_pg import PostgresHeartbeatRepository
    from devforge.adapters.driven.storage.incident_pg import PostgresIncidentRepository
    from devforge.adapters.driven.storage.state_json import JsonStateStorage
    from devforge.core.config import get_config
    from devforge.core.exceptions import ConfigurationError

    registry = TrackerRegistry()
    cfg = get_config()
    if not cfg.db_url_async:
        raise ConfigurationError("DEVFORGE_DATABASE_URL is not set (run inside devforge-net)")
    gateway = DatabaseGateway.from_config(cfg)
    heartbeat_repo = PostgresHeartbeatRepository(gateway)
    ebook_svc = "ebook-watcher"
    svc_targets = [s for s in config.critical_services if s != ebook_svc]
    health_ports: dict[str, HealthCheckPort] = {
        "svc": SystemdServiceHealthChecker(svc_targets),
        "ebook": EbookPipelineHealthChecker(ebook_svc),  # hang-aware (legacy check_ebook_pipeline)
        "alert": SystemdServiceHealthChecker(config.alert_only_targets),  # svc: prefix, alert-only
        "syssvc": SystemdSystemServiceHealthChecker(config.system_service_targets),
        "svcpod": SvcpodForwardingHealthChecker(config.svcpod_published_ports),
        "timer": SystemdTimerHealthChecker(config.timers),
        "oneshot": OneshotResultHealthChecker(config.oneshot_result_targets),
        "llm": LLMHealthChecker(
            config.llm_targets,
            day_ports=set(config.day_ports),
            latency_baseline_ms=config.llm_latency_baseline_ms,
        ),
        "system": MemoryHealthChecker(),
        "disk": DiskHealthChecker(config.disks),
        "heartbeat": HeartbeatHealthChecker(heartbeat_repo, config.heartbeat_workers),
    }
    if config.dataimpulse_enabled:
        from devforge.adapters.driven.health.dataimpulse_path import DataImpulsePathHealthChecker

        health_ports["dataimpulse"] = DataImpulsePathHealthChecker(
            status_file=config.dataimpulse_status_file,
            log_file=config.dataimpulse_log_file,
            stale_sec=config.dataimpulse_stale_sec,
            deep_stale_sec=config.dataimpulse_deep_stale_sec,
            deep_consecutive=config.dataimpulse_deep_consecutive,
        )
    check_coordinator = CheckCoordinator(registry, health_ports)
    recovery_coordinator = RecoveryCoordinator(registry)
    recovery_port = SystemdRecoveryAdapter()

    notifiers: list[NotificationPort] = [SystemdNotifier()]
    import os

    slack_token = os.environ.get("SLACK_BOT_TOKEN_KEY", "")
    if slack_token:
        notifiers.append(SlackNotifier(slack_token, os.environ.get("SLACK_CHANNEL", "")))

    incident_repo = PostgresIncidentRepository(gateway)
    state_storage = JsonStateStorage(config.state_file)

    service = WatchdogService(
        config,
        registry,
        check_coordinator,
        recovery_coordinator,
        recovery_port,
        notifiers,
        incident_repo,
        state_storage,
        dry_run=dry_run,
    )
    service.load_state()
    return service
