#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""Worker heartbeat checks (legacy checker.py:417-491)."""
from __future__ import annotations

from typing import Mapping

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.heartbeat import HeartbeatRepository
from devforge.ports.types import HealthCheck

DEFAULT_WORKER_MAX_AGE = 1800


class HeartbeatHealthChecker(HealthCheckPort):
    """Dead-man's switch for heartbeat_* rows in watchdog_pulses."""

    def __init__(self, repository: HeartbeatRepository,
                 workers: Mapping[str, int] | None = None) -> None:
        self._repository = repository
        self._workers = dict(workers or {})   # worker -> max_age_sec

    async def check_health(self) -> list[HealthCheck]:
        try:
            readings = await self._repository.list_heartbeats()
        except Exception as e:  # noqa: BLE001
            return [HealthCheck("heartbeat:db", False, f"query failed: {e}")]

        seen = {r.worker: r for r in readings}
        checks: list[HealthCheck] = []
        for worker, max_age in self._workers.items():
            row = seen.get(worker)
            if row is None:
                checks.append(HealthCheck(f"heartbeat:{worker}", False, "never beat"))
                continue
            if row.status in ("RESOLVED", "IGNORED"):
                checks.append(HealthCheck(f"heartbeat:{worker}", True, row.status))
                continue
            age = row.age_sec
            checks.append(HealthCheck(f"heartbeat:{worker}", age < max_age,
                                      f"{age}s ago" if age < max_age else f"{age}s >= {max_age}s"))
        return checks
