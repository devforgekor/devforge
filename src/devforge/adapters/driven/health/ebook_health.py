#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""ebook-watcher pipeline liveness (legacy checker.py:162-235).

Three layers: systemd active → loop process present → recent journal activity
(hang detection). The journal layer is best-effort: on parse failure it does
not false-positive a healthy service.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

DEFAULT_SERVICE = "ebook-watcher"
DEFAULT_HANG_STALE_SEC = 1800
_ACTIVITY_MARKERS = ("Cycle", "collect 완료", "저장 완료")


async def _run(cmd: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=timeout
    )


class EbookPipelineHealthChecker(HealthCheckPort):
    def __init__(
        self,
        service: str = DEFAULT_SERVICE,
        component: str | None = None,
        hang_stale_sec: int = DEFAULT_HANG_STALE_SEC,
    ) -> None:
        self._service = service
        self._component = component or f"svc:{service}"
        self._hang_stale_sec = hang_stale_sec

    async def check_health(self) -> list[HealthCheck]:
        try:
            r = await _run(["systemctl", "--user", "is-active", self._service])
            if r.stdout.strip() != "active":
                return [HealthCheck(self._component, False, f"{self._service} inactive")]

            r = await _run(["pgrep", "-f", "pipeline.py loop"])
            if not r.stdout.strip():
                return [HealthCheck(self._component, False, "pipeline.py loop process missing")]

            age = await self._last_activity_age()
            if age is not None and age > self._hang_stale_sec:
                return [
                    HealthCheck(
                        self._component, False, f"no journal activity for {int(age)}s (hang?)"
                    )
                ]
            return [HealthCheck(self._component, True, "active, loop running")]
        except Exception as e:  # noqa: BLE001
            return [HealthCheck(self._component, False, str(e))]

    async def _last_activity_age(self) -> float | None:
        """Seconds since the last activity journal line, or None if undeterminable."""
        try:
            r = await _run(["journalctl", "--user", "-u", self._service, "--no-pager", "-n", "200"])
            lines = [ln for ln in r.stdout.splitlines() if any(m in ln for m in _ACTIVITY_MARKERS)]
            if not lines:
                return None  # best-effort: cannot determine → do not false-positive
            ts_str = lines[-1].split(" devforge")[0].strip()
            last_dt = datetime.strptime(ts_str, "%b %d %H:%M:%S").replace(
                year=datetime.now(timezone.utc).year, tzinfo=timezone.utc
            )
            return (datetime.now(timezone.utc) - last_dt).total_seconds()
        except Exception:  # noqa: BLE001
            return None
