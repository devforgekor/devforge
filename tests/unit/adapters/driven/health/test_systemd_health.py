#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for systemd health adapters (C1)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.health.systemd_health import (
    OneshotResultHealthChecker,
    SystemdServiceHealthChecker,
    SystemdSystemServiceHealthChecker,
    SystemdTimerHealthChecker,
)


@pytest.mark.asyncio
async def test_all_services_active() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="active\n"))):
        checks = await SystemdServiceHealthChecker(["a", "b"]).check_health()
    assert all(c.is_healthy for c in checks)
    assert [c.component for c in checks] == ["svc:a", "svc:b"]


@pytest.mark.asyncio
async def test_inactive_service() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="inactive\n"))):
        checks = await SystemdServiceHealthChecker(["a"]).check_health()
    assert checks[0].is_healthy is False and checks[0].detail == "inactive"


@pytest.mark.asyncio
async def test_activating_service_is_healthy_when_oneshot_running() -> None:
    # legacy svc_active(): Type=oneshot의 ExecStart 진행 중 — is-active는 rc=3이다.
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="activating\n"))):
        checks = await SystemdServiceHealthChecker(["devforge-day-cycle"]).check_health()
    assert checks[0].is_healthy is True
    assert checks[0].detail == "activating"


@pytest.mark.asyncio
async def test_failed_service_is_unhealthy() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="failed\n"))):
        checks = await SystemdServiceHealthChecker(["a"]).check_health()
    assert checks[0].is_healthy is False and checks[0].detail == "failed"


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
    with (
        patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=recent + "\n"))),
        patch("devforge.adapters.driven.health.systemd_health.datetime") as dt,
    ):
        dt.strptime.side_effect = __import__("datetime").datetime.strptime
        dt.now.return_value = __import__("datetime").datetime(
            2026, 9, 22, 0, 1, tzinfo=__import__("datetime").timezone.utc)
        checks = await SystemdTimerHealthChecker({"t.timer": 2100}).check_health()
    assert checks[0].is_healthy is True


@pytest.mark.asyncio
async def test_timer_never_triggered_but_service_started_recently() -> None:
    # legacy check_timer(): LastTriggerUSec가 비어도 서비스 ExecMainStartTimestamp
    # 후보가 있으면 idle 판정한다(수동 kick 포함).
    recent = "Mon 2026-09-22 00:00:00 UTC"
    outs = [
        MagicMock(stdout="n/a\n"),
        MagicMock(stdout="n/a\n"),
        MagicMock(stdout=recent + "\n"),
    ]
    with (
        patch("asyncio.to_thread", new=AsyncMock(side_effect=outs)),
        patch("devforge.adapters.driven.health.systemd_health.datetime") as dt,
    ):
        dt.strptime.side_effect = datetime.strptime
        dt.now.return_value = datetime(2026, 9, 22, 0, 1, tzinfo=timezone.utc)
        checks = await SystemdTimerHealthChecker({"t.timer": 2100}).check_health()
    assert checks[0].is_healthy is True


@pytest.mark.asyncio
async def test_syssvc_active() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="active\n"))):
        checks = await SystemdSystemServiceHealthChecker(["caddy", "netdata"]).check_health()
    assert all(c.is_healthy for c in checks)
    assert [c.component for c in checks] == ["syssvc:caddy", "syssvc:netdata"]


@pytest.mark.asyncio
async def test_syssvc_inactive() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="inactive\n"))):
        checks = await SystemdSystemServiceHealthChecker(["caddy"]).check_health()
    assert checks[0].is_healthy is False and checks[0].detail == "inactive"


@pytest.mark.asyncio
async def test_oneshot_success() -> None:
    out = "ActiveState=inactive\nResult=success\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await OneshotResultHealthChecker(["devforge-backup.service"]).check_health()
    assert checks[0].is_healthy and checks[0].component == "oneshot:devforge-backup.service"


@pytest.mark.asyncio
async def test_oneshot_failed() -> None:
    out = "ActiveState=failed\nResult=exit-code\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await OneshotResultHealthChecker(["devforge-backup.service"]).check_health()
    assert checks[0].is_healthy is False
