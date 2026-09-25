#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/health/
"""DataImpulse path (toki31) liveness — read-only, alert-only (no recovery).

Role boundary: the watchdog detects/records/alerts only. Traffic safety-net and
usage comparison stay inside ebooklib. This adapter reads existing signals only
(status.json / collect_toki31.log mtime / process presence); it does NOT import
ebooklib, launch a browser, or query the dashboard.

Signals (in order):
  1. status.json: `sources.toki31` if present, else top-level phase/updated_at/processed
  2. collect_toki31.log mtime
  3. process presence (pgrep -f "pipeline.py collect --source toki31")

Fails-open: a missing/unparseable signal is `unknown` -> healthy (no alert);
absence of evidence is not a fault. Only a clearly stale signal is `stalled`.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck, PathStatus

DEFAULT_STATUS_FILE = "/opt/ai_data/flaresolverr/ebook_watcher/status.json"
DEFAULT_LOG_FILE = "/opt/ai_data/flaresolverr/ebook_watcher/collect_toki31.log"
DEFAULT_STALE_SEC = 1800  # 6x the 300s collect delay
DEFAULT_DEEP_STALE_SEC = 300
DEFAULT_DEEP_CONSECUTIVE = 3
DEFAULT_PROCESS_PATTERN = "pipeline.py collect --source toki31"


async def _run(cmd: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run, cmd, capture_output=True, text=True, timeout=timeout
    )


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class DataImpulsePathHealthChecker(HealthCheckPort):
    def __init__(
        self,
        status_file: str = DEFAULT_STATUS_FILE,
        log_file: str = DEFAULT_LOG_FILE,
        component: str = "dataimpulse:toki31",
        stale_sec: int = DEFAULT_STALE_SEC,
        deep_stale_sec: int = DEFAULT_DEEP_STALE_SEC,
        deep_consecutive: int = DEFAULT_DEEP_CONSECUTIVE,
        process_pattern: str = DEFAULT_PROCESS_PATTERN,
        reconcile_gap_pct: float = 20.0,
    ) -> None:
        self._status_file = Path(status_file)
        self._log_file = Path(log_file)
        self._component = component
        self._stale_sec = stale_sec
        self._deep_stale_sec = deep_stale_sec
        self._deep_consecutive = deep_consecutive
        self._process_pattern = process_pattern
        self._reconcile_gap_pct = reconcile_gap_pct
        # in-memory deep-check baseline (design §2.5; DB persistence deferred)
        self._last_processed: Optional[int] = None
        self._last_change_mono: Optional[float] = None
        self._stall_count = 0

    async def check_health(self) -> list[HealthCheck]:
        try:
            status = await self._evaluate()
        except Exception as e:  # noqa: BLE001
            # never false-positive a healthy path on checker error
            status = PathStatus(self._component, True, None, None, None, "unknown", str(e))
        return [HealthCheck(self._component, status.active, f"[{status.state}] {status.detail}")]

    async def _evaluate(self) -> PathStatus:
        structured = self._read_status()
        log_age = self._log_age()
        proc = await self._process_present()

        if structured is not None:
            return self._structured(structured, proc)
        if log_age is not None:
            if log_age <= self._stale_sec:
                return self._result(
                    True, "active", None, None, None, f"active (log age={int(log_age)}s)"
                )
            return self._result(
                False, "stalled", None, None, None, f"stalled (log age={int(log_age)}s)"
            )
        if proc:
            return self._result(True, "active", None, None, None, "active (process present)")
        if self._status_file.exists():
            return self._result(
                True, "unknown", None, None, None, "status unparseable, no other signal"
            )
        return self._result(False, "absent", None, None, None, "no status/log/process")

    def _structured(self, status: dict[str, Any], proc: bool) -> PathStatus:
        sources = status.get("sources")
        node = sources.get("toki31") if isinstance(sources, dict) else None
        if not isinstance(node, dict):
            node = status  # fall back to top-level (current ebooklib writes sources={})
        phase = node.get("phase") or status.get("phase")
        processed = node.get("processed")
        if not isinstance(processed, int):
            processed = None
        ts = _parse_ts(node.get("updated_at") or status.get("updated_at"))
        signal = self._traffic_signal(status)
        if signal is not None:
            return self._result(False, "degraded", ts, processed, phase, signal)
        if ts is None:
            if proc:
                return self._result(
                    True, "active", None, processed, phase, "active (process present, no timestamp)"
                )
            return self._result(
                True, "unknown", None, processed, phase, "status missing updated_at"
            )
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        if age > self._stale_sec:
            return self._result(
                False, "stalled", ts, processed, phase, f"stalled (phase={phase}, age={int(age)}s)"
            )
        deep = self._deep_stall(processed)
        if deep is not None:
            return self._result(False, "stalled", ts, processed, phase, deep)
        return self._result(
            True, "active", ts, processed, phase, f"active (phase={phase}, age={int(age)}s)"
        )

    def _deep_stall(self, processed: Optional[int]) -> Optional[str]:
        """Return a stall message if `processed` has not advanced for N deep windows."""
        if processed is None:
            self._last_processed = None
            self._stall_count = 0
            return None
        now = time.monotonic()
        if self._last_processed is None or processed != self._last_processed:
            self._last_processed = processed
            self._last_change_mono = now
            self._stall_count = 0
            return None
        if self._last_change_mono is None:
            self._last_change_mono = now
            return None
        if now - self._last_change_mono < self._deep_stale_sec:
            return None
        self._stall_count += 1
        if self._stall_count >= self._deep_consecutive:
            return f"deep stall: processed={processed} unchanged for >{self._deep_stale_sec}s x{self._stall_count}"
        return None

    def _traffic_signal(self, status: dict[str, Any]) -> Optional[str]:
        """status.json.traffic 요약 기반 경보 — 없으면 None(정상).

        비페이징(alert-only)·상태 전이 경보 표준: 요약이 임계를 넘을 때만 degrade하고,
        중복/억제/전이는 watchdog 상태기계가 담당한다. 소유/범위 메타를 문구에 포함.
        """
        traffic = status.get("traffic")
        if not isinstance(traffic, dict):
            return None
        summary = traffic.get("summary")
        if isinstance(summary, dict):
            level = summary.get("quota_level")
            if level in ("warn", "critical"):
                return (
                    f"quota {level}: {summary.get('used_mb_guard')}/"
                    f"{summary.get('daily_limit_mb')}MB, chapters "
                    f"{summary.get('chapters_today')}/{summary.get('daily_chapter_cap')}, "
                    f"forecast {summary.get('forecast_mb')}MB "
                    "(owner=ebooklib scope=traffic)"
                )
            if summary.get("burn_level") in ("warn", "critical"):
                return (
                    f"burn-rate {summary.get('burn_level')}: "
                    f"1h={summary.get('burn_rate_1h')} 6h={summary.get('burn_rate_6h')} "
                    "(owner=ebooklib scope=traffic)"
                )
            if summary.get("exceeded"):
                return (
                    f"daily cap reached: {summary.get('used_mb_guard')}/"
                    f"{summary.get('daily_limit_mb')}MB (owner=ebooklib scope=traffic)"
                )
        if traffic.get("bucket_anomaly_stop") or traffic.get("calibration_emergency"):
            return "traffic safety-net stop flag set (owner=ebooklib scope=traffic)"
        reconcile = traffic.get("reconcile")
        if isinstance(reconcile, dict):
            gap = reconcile.get("gap_pct")
            if isinstance(gap, (int, float)) and abs(gap) >= self._reconcile_gap_pct:
                return (
                    f"API/bucket reconcile gap {gap}% (day={reconcile.get('day')}) "
                    "(owner=ebooklib scope=traffic)"
                )
        return None

    def _read_status(self) -> Optional[dict[str, Any]]:
        try:
            data = json.loads(self._status_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _log_age(self) -> Optional[float]:
        try:
            return time.time() - self._log_file.stat().st_mtime
        except OSError:
            return None

    async def _process_present(self) -> bool:
        try:
            r = await _run(["pgrep", "-f", self._process_pattern])
            return bool(r.stdout.strip())
        except Exception:  # noqa: BLE001
            return False

    def _result(
        self,
        active: bool,
        state: str,
        last_seen: Optional[datetime],
        processed: Optional[int],
        phase: Optional[str],
        detail: str,
    ) -> PathStatus:
        return PathStatus(self._component, active, last_seen, processed, phase, state, detail)
