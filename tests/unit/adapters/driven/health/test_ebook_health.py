#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for ebook-watcher pipeline liveness checker (gap #3)."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from devforge.adapters.driven.health import ebook_health
from devforge.adapters.driven.health.ebook_health import EbookPipelineHealthChecker


def _mock_run(monkeypatch: pytest.MonkeyPatch, *, active: str, pgrep: str,
              journal: str = "") -> None:
    async def _run(cmd: list[str], timeout: int = 8) -> Any:
        if "is-active" in cmd:
            return SimpleNamespace(stdout=active)
        if "pgrep" in cmd:
            return SimpleNamespace(stdout=pgrep)
        if "journalctl" in cmd:
            return SimpleNamespace(stdout=journal)
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(ebook_health, "_run", _run)


@pytest.mark.asyncio
async def test_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, active="inactive\n", pgrep="")
    checks = await EbookPipelineHealthChecker().check_health()
    assert checks[0].is_healthy is False and "inactive" in checks[0].detail
    assert checks[0].component == "svc:ebook-watcher"


@pytest.mark.asyncio
async def test_active_no_process(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, active="active\n", pgrep="")
    checks = await EbookPipelineHealthChecker().check_health()
    assert checks[0].is_healthy is False and "process missing" in checks[0].detail


@pytest.mark.asyncio
async def test_ok_when_journal_undeterminable(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, active="active\n", pgrep="12345\n", journal="")
    checks = await EbookPipelineHealthChecker().check_health()
    assert checks[0].is_healthy is True


@pytest.mark.asyncio
async def test_hang_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_run(monkeypatch, active="active\n", pgrep="12345\n", journal="x")

    async def fake_age(self: EbookPipelineHealthChecker) -> float:
        return 9999.0

    monkeypatch.setattr(EbookPipelineHealthChecker, "_last_activity_age", fake_age)
    checks = await EbookPipelineHealthChecker(hang_stale_sec=60).check_health()
    assert checks[0].is_healthy is False and "hang" in checks[0].detail
