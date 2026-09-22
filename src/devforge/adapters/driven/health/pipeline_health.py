#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""Pipeline health check adapter (async via database)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheckResult


class PipelineHeartbeatCheck(HealthCheckPort):
    """Health check for pipeline heartbeats."""

    def __init__(self, db_session_factory: Any, stale_threshold_sec: int = 300) -> None:
        self._session_factory = db_session_factory
        self._threshold = stale_threshold_sec

    async def check_health(self) -> HealthCheckResult:
        """Check if pipeline heartbeats are recent."""
        stale = []
        now = datetime.now(timezone.utc)

        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    """
                    SELECT name, last_heartbeat_at
                    FROM pulse_tracking
                    WHERE resolved_at IS NULL
                    ORDER BY last_heartbeat_at DESC
                    """
                )
                pulses = result.fetchall()

            for name, last_heartbeat in pulses:
                if last_heartbeat is None:
                    stale.append(name)
                    continue

                elapsed = (now - last_heartbeat).total_seconds()
                if elapsed > self._threshold:
                    stale.append(f"{name} ({int(elapsed)}s)")

        except Exception as e:
            return HealthCheckResult(
                component="pipeline-heartbeats",
                ok=False,
                detail=f"Query failed: {e}",
                timestamp=now,
            )

        if stale:
            return HealthCheckResult(
                component="pipeline-heartbeats",
                ok=False,
                detail=f"Stale heartbeats: {', '.join(stale)}",
                timestamp=now,
            )

        return HealthCheckResult(
            component="pipeline-heartbeats",
            ok=True,
            detail=f"{len(pulses)} active pipelines",
            timestamp=now,
        )
