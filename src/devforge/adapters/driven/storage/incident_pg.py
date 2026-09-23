#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/storage/
"""Postgres incidents (legacy incidents.py:112-227)."""

from __future__ import annotations

import asyncio
import re
import subprocess
from typing import Any, Optional

from sqlalchemy import func, insert, select, update

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.domain.models import WatchdogIncident
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.types import Incident

REOPEN_WINDOW_SEC = 3600
_CONTEXT_MAX_LEN = 500
_SECRET_RE = re.compile(r"(token|key|password|secret|passwd)\s*[=:]\s*\S+", re.IGNORECASE)


def _to_incident(row: WatchdogIncident) -> Incident:
    return Incident(
        id=row.id,
        dedup_key=row.dedup_key,
        component=row.component,
        status=row.status,
        symptom=row.symptom,
        context=row.context,
        detected_at=row.detected_at,
        last_seen_at=row.last_seen_at,
        action=row.action,
        action_result=row.action_result,
        action_at=row.action_at,
        resolved_at=row.resolved_at,
        fail_count=row.fail_count,
        reopen_count=row.reopen_count,
    )


class PostgresIncidentRepository(IncidentRepository):
    def __init__(self, gateway: DatabaseGateway) -> None:
        self._gateway = gateway

    async def record_detect(
        self, component: str, event_type: str, detail: str, unit: Optional[str] = None
    ) -> Optional[int]:
        dedup = f"{component}:{event_type}"
        context = await _capture_context(unit)
        async with self._gateway.session() as session:
            open_row = (
                await session.execute(
                    select(WatchdogIncident)
                    .where(WatchdogIncident.dedup_key == dedup, WatchdogIncident.status == "open")
                    .order_by(WatchdogIncident.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if open_row is not None:
                await session.execute(
                    update(WatchdogIncident)
                    .where(WatchdogIncident.id == open_row.id)
                    .values(
                        fail_count=WatchdogIncident.fail_count + 1,
                        symptom=detail[:500],
                        last_seen_at=func.now(),
                    )
                )
                return open_row.id  # type: ignore[no-any-return]
            recent = (
                await session.execute(
                    select(WatchdogIncident)
                    .where(
                        WatchdogIncident.dedup_key == dedup,
                        WatchdogIncident.status == "resolved",
                        WatchdogIncident.resolved_at
                        > func.now() - func.make_interval(0, 0, 0, 0, 0, 0, REOPEN_WINDOW_SEC),
                    )
                    .order_by(WatchdogIncident.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if recent is not None:
                await session.execute(
                    update(WatchdogIncident)
                    .where(WatchdogIncident.id == recent.id)
                    .values(
                        status="open",
                        reopen_count=WatchdogIncident.reopen_count + 1,
                        fail_count=WatchdogIncident.fail_count + 1,
                        resolved_at=None,
                        detected_at=func.now(),
                        last_seen_at=func.now(),
                        symptom=detail[:500],
                    )
                )
                return recent.id  # type: ignore[no-any-return]
            new_row = (
                await session.execute(
                    insert(WatchdogIncident)
                    .values(
                        dedup_key=dedup,
                        component=component,
                        status="open",
                        symptom=detail[:500],
                        context=context,
                    )
                    .returning(WatchdogIncident.id)
                )
            ).scalar_one()
            return new_row  # type: ignore[no-any-return]

    async def record_action(self, incident_id: Optional[int], action: str, ok: bool) -> None:
        if incident_id is None:
            return
        values: dict[str, Any] = {
            "action": action,
            "action_result": "success" if ok else "fail",
            "action_at": func.now(),
        }
        if ok:
            values.update(status="resolved", resolved_at=func.now())
        async with self._gateway.session() as session:
            await session.execute(
                update(WatchdogIncident).where(WatchdogIncident.id == incident_id).values(**values)
            )

    async def resolve_if_open(self, component: str) -> None:
        async with self._gateway.session() as session:
            await session.execute(
                update(WatchdogIncident)
                .where(
                    WatchdogIncident.dedup_key.like(f"{component}:%"),
                    WatchdogIncident.status == "open",
                )
                .values(
                    status="resolved",
                    resolved_at=func.now(),
                    action=func.coalesce(WatchdogIncident.action, "auto-recovered"),
                    action_result="success",
                    action_at=func.now(),
                )
            )

    async def find_open(self, component: Optional[str] = None) -> list[Incident]:
        stmt = select(WatchdogIncident).where(WatchdogIncident.status == "open")
        if component is not None:
            stmt = stmt.where(WatchdogIncident.component == component)
        async with self._gateway.session() as session:
            rows = (
                (await session.execute(stmt.order_by(WatchdogIncident.detected_at.desc())))
                .scalars()
                .all()
            )
        return [_to_incident(r) for r in rows]


async def _capture_context(unit: Optional[str]) -> Optional[str]:
    """Best-effort bounded, masked diagnostic context (legacy incidents.py:87-109).

    Runs outside the DB session so the subprocess never holds a connection.
    """
    if not unit:
        return None
    try:
        r = await asyncio.to_thread(
            subprocess.run,
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=ActiveState,SubState,Result,ExecMainStatus",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        text = (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return None
    if not text:
        return None
    return _SECRET_RE.sub(r"\1=***", text)[:_CONTEXT_MAX_LEN]
