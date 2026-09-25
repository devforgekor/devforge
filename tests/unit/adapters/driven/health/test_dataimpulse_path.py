#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for DataImpulse path (toki31) liveness checker.

Covers: active (fresh status), stalled (stale), absent (no signal), unknown
(fails-open, no alert), top-level fallback (sources={}), deep stall, and
process-present fallback. No ebooklib import, no side effects.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from devforge.adapters.driven.health import dataimpulse_path
from devforge.adapters.driven.health.dataimpulse_path import DataImpulsePathHealthChecker


def _mock_pgrep(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    async def _run(cmd: list[str], timeout: int = 8) -> Any:
        if "pgrep" in cmd:
            return SimpleNamespace(stdout="12345\n" if present else "")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(dataimpulse_path, "_run", _run)


def _write_status(
    path: Path,
    *,
    phase: str = "collect",
    age_sec: float = 0.0,
    processed: int | None = None,
    sources: dict | None = None,
) -> None:
    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_sec)).isoformat()
    data: dict[str, Any] = {"phase": phase, "updated_at": ts, "sources": sources or {}}
    if processed is not None:
        data["processed"] = processed
    path.write_text(json.dumps(data), encoding="utf-8")


def _checker(tmp_path: Path, **kwargs: Any) -> DataImpulsePathHealthChecker:
    return DataImpulsePathHealthChecker(
        status_file=str(tmp_path / "status.json"),
        log_file=str(tmp_path / "collect_toki31.log"),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_active_when_status_fresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_status(tmp_path / "status.json", phase="collect", age_sec=5)
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is True and "[active]" in checks[0].detail
    assert checks[0].component == "dataimpulse:toki31"


@pytest.mark.asyncio
async def test_stalled_when_status_stale(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_status(tmp_path / "status.json", age_sec=4000)
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False and "[stalled]" in checks[0].detail


@pytest.mark.asyncio
async def test_absent_when_no_signal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_pgrep(monkeypatch, present=False)
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False and "[absent]" in checks[0].detail


@pytest.mark.asyncio
async def test_unknown_when_status_unparseable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _mock_pgrep(monkeypatch, present=False)
    (tmp_path / "status.json").write_text("{not json", encoding="utf-8")
    checks = await _checker(tmp_path).check_health()
    # fails-open: unparseable signal -> no alert
    assert checks[0].is_healthy is True and "[unknown]" in checks[0].detail


@pytest.mark.asyncio
async def test_top_level_fallback_when_sources_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_status(tmp_path / "status.json", phase="loop", age_sec=5, sources={})
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is True and "[active]" in checks[0].detail


@pytest.mark.asyncio
async def test_active_when_process_present_no_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=True)
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is True and "[active]" in checks[0].detail


@pytest.mark.asyncio
async def test_deep_stall_after_consecutive_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_status(tmp_path / "status.json", age_sec=5, processed=127)
    checker = _checker(tmp_path, deep_stale_sec=0, deep_consecutive=2)
    results = [(await checker.check_health())[0].is_healthy for _ in range(3)]
    # first two establish the baseline, third reports the deep stall
    assert results[0] is True and results[1] is True and results[2] is False


def _write_traffic_status(path: Path, traffic: dict) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"phase": "collect", "updated_at": ts, "sources": {}, "traffic": traffic}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_degraded_when_quota_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(
        tmp_path / "status.json",
        {
            "summary": {
                "quota_level": "warn",
                "used_mb_guard": 330,
                "daily_limit_mb": 400,
                "chapters_today": 1100,
                "daily_chapter_cap": 2000,
                "forecast_mb": 360,
                "exceeded": False,
            }
        },
    )
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False
    assert "[degraded]" in checks[0].detail and "quota warn" in checks[0].detail


@pytest.mark.asyncio
async def test_degraded_when_daily_cap_reached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(
        tmp_path / "status.json",
        {"summary": {"quota_level": "ok", "used_mb_guard": 400, "daily_limit_mb": 400, "exceeded": True}},
    )
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False and "daily cap reached" in checks[0].detail


@pytest.mark.asyncio
async def test_degraded_when_bucket_stop_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(tmp_path / "status.json", {"bucket_anomaly_stop": True})
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False and "safety-net stop" in checks[0].detail


@pytest.mark.asyncio
async def test_degraded_when_reconcile_gap_over_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(
        tmp_path / "status.json",
        {"reconcile": {"day": "2026-09-23", "gap_pct": 25.0}},
    )
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False and "reconcile gap" in checks[0].detail


@pytest.mark.asyncio
async def test_reconcile_gap_threshold_is_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(
        tmp_path / "status.json",
        {"reconcile": {"day": "2026-09-23", "gap_pct": 15.0}},
    )
    assert (await _checker(tmp_path).check_health())[0].is_healthy is True
    strict = _checker(tmp_path, reconcile_gap_pct=10.0)
    assert (await strict.check_health())[0].is_healthy is False


@pytest.mark.asyncio
async def test_active_when_traffic_normal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(
        tmp_path / "status.json",
        {"summary": {"quota_level": "ok", "exceeded": False}, "reconcile": {"gap_pct": 5.0}},
    )
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is True and "[active]" in checks[0].detail


@pytest.mark.asyncio
async def test_degraded_when_burn_rate_warn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _mock_pgrep(monkeypatch, present=False)
    _write_traffic_status(
        tmp_path / "status.json",
        {
            "summary": {
                "quota_level": "ok",
                "burn_level": "warn",
                "burn_rate_1h": 1.2,
                "burn_rate_6h": 1.1,
                "exceeded": False,
            }
        },
    )
    checks = await _checker(tmp_path).check_health()
    assert checks[0].is_healthy is False and "burn-rate" in checks[0].detail
