#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/storage/
"""Postgres incidents (legacy incidents.py:112-227)."""

from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, insert, or_, select, update

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.core.telemetry import correlation
from devforge.domain.models import WatchdogIncident
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.types import Incident

REOPEN_WINDOW_SEC = 3600
_JOURNAL_TAIL_LINES = 40
_SYSTEMD_PROPS = "ActiveState,SubState,Result,ExecMainStatus,NRestarts"
_CONTAINER_LOG_MAX = 4000

# 4-pattern masking (error-record-analysis-design §1.4).
_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"), r"\1***"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "sk-***"),
    (re.compile(r"(?i)(password|passwd|token|secret|api[_-]?key)\s*[=:]\s*\S+"), r"\1=***"),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "***PRIVATE KEY***",
    ),
]


def _mask(text: str) -> str:
    for pattern, repl in _MASK_PATTERNS:
        text = pattern.sub(repl, text)
    return text


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
        context_jsonb=row.context_jsonb,
        action_error=row.action_error,
    )


class PostgresIncidentRepository(IncidentRepository):
    def __init__(self, gateway: DatabaseGateway) -> None:
        self._gateway = gateway

    async def record_detect(
        self, component: str, event_type: str, detail: str, unit: Optional[str] = None
    ) -> Optional[int]:
        dedup = f"{component}:{event_type}"
        context_jsonb = await _capture_context_jsonb(component, unit)
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
                        context_jsonb=context_jsonb,  # repeat: refresh diagnostics
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
                        context_jsonb=context_jsonb,  # reopen: refresh diagnostics
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
                        context_jsonb=context_jsonb,
                    )
                    .returning(WatchdogIncident.id)
                )
            ).scalar_one()
            return new_row  # type: ignore[no-any-return]

    async def record_action(
        self,
        incident_id: Optional[int],
        action: str,
        ok: bool,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        if incident_id is None:
            return
        values: dict[str, Any] = {
            "action": action,
            "action_result": "success" if ok else "fail",
            "action_at": func.now(),
        }
        if error is not None:
            values["action_error"] = error
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

    async def find_since(self, since: datetime) -> list[Incident]:
        """Incidents relevant to an SLI window: detected in-window OR still open.

        [WHY] An incident opened before the window and never resolved still spans
        the window, so excluding it would under-count downtime (false-good SLI).
        """
        stmt = (
            select(WatchdogIncident)
            .where(
                or_(
                    WatchdogIncident.detected_at >= since,
                    WatchdogIncident.status == "open",
                )
            )
            .order_by(WatchdogIncident.detected_at.asc())
        )
        async with self._gateway.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_to_incident(r) for r in rows]


async def _run_capture(cmd: list[str], timeout: int) -> Optional[str]:
    """Run a read-only capture command; return stdout or None (best-effort)."""
    try:
        r = await asyncio.to_thread(
            subprocess.run, cmd, capture_output=True, text=True, timeout=timeout
        )
    except Exception:  # noqa: BLE001
        return None
    out = (r.stdout or "").strip()
    return out or None


async def _capture_context_jsonb(component: str, unit: Optional[str]) -> dict[str, Any]:
    """Structured, masked, bounded diagnostics (error-record-analysis-design §1.3-1.4).

    Reads only (systemctl show / journalctl tail / podman logs). Best-effort:
    a missing tool (e.g. inside the v2 container) yields an absent section, not
    an error. Runs outside the DB session so subprocesses never hold a connection.
    """
    unit = unit or (component.split(":", 1)[1] if ":" in component else component)
    # [WHY] "degraded must be visible": record which sections were captured vs
    # absent so a partial context is never mistaken for a complete one.
    is_container = unit.startswith("container-")
    capture_status: dict[str, str] = {
        "systemd": "absent",
        "journal": "absent",
        "container": "n/a" if not is_container else "absent",
    }
    ctx: dict[str, Any] = {
        "schema_version": 1,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "component": component,
        "unit": unit,
        "truncated": False,
    }
    # [WHY] correlate the incident with the active trace/run (2026-standard-gap §9).
    ctx.update(correlation())

    show_cmd = ["systemctl", "--user", "show", unit, f"--property={_SYSTEMD_PROPS}"]
    ctx["command"] = show_cmd
    out = await _run_capture(show_cmd, timeout=3)
    if out:
        props: dict[str, str] = {}
        for line in out.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                props[key] = _mask(value)
        ctx["systemd"] = props
        capture_status["systemd"] = "ok"

    journal = await _run_capture(
        ["journalctl", "--user", "-u", unit, "--no-pager", "-n", str(_JOURNAL_TAIL_LINES)],
        timeout=5,
    )
    if journal:
        ctx["journal_tail"] = [_mask(ln) for ln in journal.splitlines() if ln.strip()]
        capture_status["journal"] = "ok"

    if is_container:
        container = unit[len("container-") :]
        logs = await _run_capture(["podman", "logs", "--tail", "40", container], timeout=5)
        if logs:
            masked = _mask(logs)
            if len(masked) > _CONTAINER_LOG_MAX:
                masked = masked[-_CONTAINER_LOG_MAX:]
                ctx["truncated"] = True
            ctx["container"] = {"logs_tail": masked}
            capture_status["container"] = "ok"

    ctx["capture_status"] = capture_status
    ctx["degraded"] = [k for k, v in capture_status.items() if v == "absent"]
    return ctx
