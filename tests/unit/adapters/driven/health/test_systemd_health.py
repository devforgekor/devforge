#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for systemd health adapters (C1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.health.systemd_health import (
    SystemdServiceHealthChecker,
    SystemdTimerHealthChecker,
)


@pytest.mark.asyncio
async def test_all_services_active() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        checks = await SystemdServiceHealthChecker(["a", "b"]).check_health()
    assert all(c.is_healthy for c in checks)
    assert [c.component for c in checks] == ["svc:a", "svc:b"]


@pytest.mark.asyncio
async def test_inactive_service() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=3))):
        checks = await SystemdServiceHealthChecker(["a"]).check_health()
    assert checks[0].is_healthy is False and "inactive" in checks[0].detail


@pytest.mark.asyncio
async def test_prefix_override() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        checks = await SystemdServiceHealthChecker(["x"], prefix="container").check_health()
    assert checks[0].component == "container:x"


@pytest.mark.asyncio
async def test_timer_never_triggered() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="n/a\n"))):
        checks = await SystemdTimerHealthChecker({"t.timer": 100}).check_health()
    assert checks[0].is_healthy is False and checks[0].component == "timer:t.timer"


@pytest.mark.asyncio
async def test_timer_recent_ok() -> None:
    recent = "Mon 2026-09-22 00:00:00 UTC"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=recent + "\n"))):
        with patch("devforge.adapters.driven.health.systemd_health.datetime") as dt:
            dt.strptime.side_effect = __import__("datetime").datetime.strptime
            dt.now.return_value = __import__("datetime").datetime(
                2026, 9, 22, 0, 1, tzinfo=__import__("datetime").timezone.utc)
            checks = await SystemdTimerHealthChecker({"t.timer": 2100}).check_health()
    assert checks[0].is_healthy is True
