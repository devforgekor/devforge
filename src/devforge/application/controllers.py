#!/usr/bin/env python3.12
# Status: experimental
# Path: application/watchdog_service.py (B dispatch)
"""Remediation controllers (detection-remediation-implementation-guide §5).

B `CatchupController` — level-based reconcile for `oneshot:`/`timer:` incidents:
run a missed oneshot or kick a stale timer, deduped by an in-memory run log so
the same unit is not re-run within `window_sec`. A `fix` handling stays in the
existing recovery path (routing canary, S2).
"""

from __future__ import annotations

import time
from typing import Optional

from devforge.core.logging import get_logger
from devforge.domain.watchdog.governance import MAX_ATTEMPTS_DEFAULT, escalate_needed
from devforge.domain.watchdog.routing import route
from devforge.ports.catchup import CatchupPort
from devforge.ports.incident_repository import IncidentRepository

_log = get_logger(__name__)


class CatchupController:
    def __init__(
        self,
        catchup_port: CatchupPort,
        incidents: IncidentRepository,
        window_sec: int = 600,
        max_attempts: int = MAX_ATTEMPTS_DEFAULT,
    ) -> None:
        self._port = catchup_port
        self._incidents = incidents
        self._window_sec = window_sec
        self._max_attempts = max_attempts
        self._last_run: dict[str, float] = {}

    def ran_recently(self, unit: str) -> bool:
        ts = self._last_run.get(unit)
        return ts is not None and (time.monotonic() - ts) < self._window_sec

    async def reconcile(
        self,
        component: str,
        event_type: str,
        inc_id: Optional[int] = None,
        fail_count: int = 0,
    ) -> str:
        """Returns: skip | already-ran | ran | retry | escalate."""
        decision = route(component, event_type, "")
        if decision.logic != "catchup":
            return "skip"
        if escalate_needed(fail_count, self._max_attempts):
            if inc_id is not None:
                await self._incidents.record_action(inc_id, "catchup:escalate", False)
            return "escalate"
        unit = component.split(":", 1)[1] if ":" in component else component
        if self.ran_recently(unit):
            return "already-ran"
        try:
            if decision.kind == "oneshot_run":
                ok = await self._port.run_oneshot(unit)
            else:
                ok = await self._port.kick_timer(unit)
        except Exception as e:  # noqa: BLE001 — a failed catch-up must not crash the cycle
            _log.warning("catchup_failed", component=component, error=str(e))
            ok = False
        self._last_run[unit] = time.monotonic()
        if inc_id is not None:
            await self._incidents.record_action(inc_id, f"catchup:{decision.kind}", ok)
        return "ran" if ok else "retry"
