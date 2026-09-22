#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""PostgreSQL incident repository (async)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.types import Incident


class PostgreSQLIncidentRepository(IncidentRepository):
    """PostgreSQL implementation of incident repository."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def save(self, incident: Incident) -> Incident:
        """Save new incident."""
        query = """
            INSERT INTO watchdog_incidents
            (component, severity, detail, created_at)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """

        async with self._session_factory() as session:
            result = await session.execute(
                query,
                incident.component,
                incident.severity,
                incident.detail,
                incident.created_at,
            )
            incident_id = result.fetchone()[0]

        return Incident(
            id=incident_id,
            component=incident.component,
            severity=incident.severity,
            detail=incident.detail,
            created_at=incident.created_at,
            resolved_at=None,
            resolution_note=None,
        )

    async def resolve(self, incident_id: int, note: str) -> bool:
        """Resolve incident."""
        query = """
            UPDATE watchdog_incidents
            SET resolved_at = $1, resolution_note = $2
            WHERE id = $3 AND resolved_at IS NULL
        """

        async with self._session_factory() as session:
            result = await session.execute(
                query,
                datetime.now(timezone.utc),
                note,
                incident_id,
            )
            return bool(result.rowcount > 0)

    async def find_open(self, component: str | None = None) -> list[Incident]:
        """Find open incidents."""
        if component:
            query = """
                SELECT id, component, severity, detail, created_at, resolved_at, resolution_note
                FROM watchdog_incidents
                WHERE resolved_at IS NULL AND component = $1
                ORDER BY created_at DESC
            """
            params = [component]
        else:
            query = """
                SELECT id, component, severity, detail, created_at, resolved_at, resolution_note
                FROM watchdog_incidents
                WHERE resolved_at IS NULL
                ORDER BY created_at DESC
            """
            params = []

        async with self._session_factory() as session:
            result = await session.execute(query, *params)
            rows = result.fetchall()

        return [
            Incident(
                id=row[0],
                component=row[1],
                severity=row[2],
                detail=row[3],
                created_at=row[4],
                resolved_at=row[5],
                resolution_note=row[6],
            )
            for row in rows
        ]
