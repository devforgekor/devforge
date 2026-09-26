#!/usr/bin/env python3
# Status: experimental
# Path: application/error_analysis.py (wired via cli.py composition root)
"""Postgres reader for error-record analysis (§2): watchdog_incidents L1+L2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import text

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.ports.error_analysis import IncidentEvidence

_QUERY = text(
    "SELECT id, component, status, symptom, fail_count, reopen_count, "
    "detected_at::text AS detected_at, context_jsonb "
    "FROM watchdog_incidents "
    "WHERE detected_at >= :since "
    "ORDER BY detected_at DESC "
    "LIMIT :limit"
)


def as_datetime(since_iso: str) -> datetime:
    """Parse an ISO string to an aware datetime (asyncpg rejects str params)."""
    since = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since


class PostgresErrorAnalysisRepository:
    def __init__(self, gateway: DatabaseGateway, limit: int = 500) -> None:
        self._gateway = gateway
        self._limit = limit

    async def list_incidents(self, since_iso: str) -> list[IncidentEvidence]:
        since = as_datetime(since_iso)
        async with self._gateway.session() as session:
            result = await session.execute(_QUERY, {"since": since, "limit": self._limit})
            rows = list(result.mappings())
        return [
            IncidentEvidence(
                incident_id=int(r["id"]),
                component=str(r["component"]),
                status=str(r["status"]),
                symptom=r["symptom"],
                fail_count=int(r["fail_count"] or 1),
                reopen_count=int(r["reopen_count"] or 0),
                detected_at=str(r["detected_at"]),
                context=cast("dict[str, Any]", r["context_jsonb"] or {}),
            )
            for r in rows
        ]
