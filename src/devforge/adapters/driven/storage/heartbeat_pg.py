#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/storage/
"""Postgres heartbeat repository (legacy checker.py:417-491)."""
from __future__ import annotations

from sqlalchemy import text

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.ports.heartbeat import HeartbeatReading

_QUERY = text(
    "SELECT pulse_id, status, "
    "EXTRACT(EPOCH FROM (now() - created_at))::int AS age "
    "FROM watchdog_pulses WHERE pulse_id LIKE 'heartbeat\\_%' ESCAPE '\\'"
)


class PostgresHeartbeatRepository:
    def __init__(self, gateway: DatabaseGateway) -> None:
        self._gateway = gateway

    async def list_heartbeats(self) -> list[HeartbeatReading]:
        async with self._gateway.session() as session:
            result = await session.execute(_QUERY)
            rows = list(result.mappings())
        return [
            HeartbeatReading(
                worker=str(r["pulse_id"]).removeprefix("heartbeat_"),
                status=str(r["status"]),
                age_sec=int(r["age"] or 0),
            )
            for r in rows
        ]
