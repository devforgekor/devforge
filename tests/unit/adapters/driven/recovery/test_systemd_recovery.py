#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/recovery/
"""Tests for systemd recovery adapter (D3)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
from devforge.ports.types import RecoveryAction


@pytest.mark.asyncio
async def test_restart_service() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        ok = await SystemdRecoveryAdapter().execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is True


@pytest.mark.asyncio
async def test_restart_failure() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=1))):
        ok = await SystemdRecoveryAdapter().execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_timer_kick_starts_service() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))) as t:
        await SystemdRecoveryAdapter().execute_recovery(
            RecoveryAction("timer:devforge-day-cycle.timer", "timer_kick", "delay"))
    cmd = t.call_args[0][1]
    assert "devforge-day-cycle.service" in cmd


@pytest.mark.asyncio
async def test_probe_failure_overrides_ok() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        with patch("asyncio.sleep", new=AsyncMock()):
            probe = AsyncMock(return_value=False)
            ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
                RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_unknown_kind_false() -> None:
    assert await SystemdRecoveryAdapter().execute_recovery(
        RecoveryAction("svc:x", "unknown", "r")) is False
