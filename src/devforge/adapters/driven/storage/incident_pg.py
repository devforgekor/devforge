#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/storage/*, application/watchdog_service.py
"""PostgreSQL incident repository adapter."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from devforge.domain.watchdog.model import Incident


class PostgreSQLIncidentRepository:
    """PostgreSQL implementation of incident repository (async-compatible)."""

    def __init__(self, db_session_factory: Any) -> None:
        self._session_factory = db_session_factory

    async def save(self, incident: Incident) -> Incident:
        async with self._session_factory() as session:
            result = await session.execute(
                """
                INSERT INTO watchdog_incidents (component, severity, detail, created_at)
                VALUES (:component, :severity, :detail, :created_at)
                RETURNING id
                """,
                {
                    "component": incident.component,
                    "severity": incident.severity,
                    "detail": incident.detail,
                    "created_at": incident.created_at,
                },
            )
            row = result.fetchone()
            return Incident(
                id=row[0],
                component=incident.component,
                severity=incident.severity,
                detail=incident.detail,
                created_at=incident.created_at,
            )

    async def resolve(self, incident_id: int, note: str) -> bool:
        async with self._session_factory() as session:
            result = await session.execute(
                """
                UPDATE watchdog_incidents
                SET resolved_at = :now, resolution_note = :note
                WHERE id = :id AND resolved_at IS NULL
                """,
                {"now": datetime.now(timezone.utc), "note": note, "id": incident_id},
            )
            return bool(result.rowcount > 0)

    async def find_open(self, component: str | None = None) -> list[Incident]:
        async with self._session_factory() as session:
            if component:
                result = await session.execute(
                    """
                    SELECT id, component, severity, detail, created_at, resolved_at, resolution_note
                    FROM watchdog_incidents
                    WHERE resolved_at IS NULL AND component = :component
                    ORDER BY created_at DESC
                    """,
                    {"component": component},
                )
            else:
                result = await session.execute(
                    """
                    SELECT id, component, severity, detail, created_at, resolved_at, resolution_note
                    FROM watchdog_incidents
                    WHERE resolved_at IS NULL
                    ORDER BY created_at DESC
                    """
                )
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
