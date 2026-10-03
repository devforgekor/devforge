#!/usr/bin/env python3.12
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
    with (
        patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        probe = AsyncMock(return_value=False)
        ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_unknown_kind_false() -> None:
    assert await SystemdRecoveryAdapter().execute_recovery(
        RecoveryAction("svc:x", "unknown", "r")) is False


@pytest.mark.asyncio
async def test_restart_clears_start_limit_before_start() -> None:
    """start limit 으로 park 된 유닛은 reset-failed 없이는 어떤 재시도도 거부된다."""
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))) as t:
        await SystemdRecoveryAdapter().execute_recovery(
            RecoveryAction("svc:container-x", "container", "down"))
    cmds = [c[0][1] for c in t.call_args_list]
    assert ["systemctl", "--user", "reset-failed", "container-x"] in cmds
    assert ["systemctl", "--user", "start", "container-x"] in cmds
    assert cmds.index(["systemctl", "--user", "reset-failed", "container-x"]) < cmds.index(
        ["systemctl", "--user", "start", "container-x"]
    )


@pytest.mark.asyncio
async def test_verification_requires_two_consecutive_passes() -> None:
    """한 번의 성공만으로는 resolved 로 보지 않는다 (M-of-N)."""
    with (
        patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        probe = AsyncMock(side_effect=[False, True, True])
        ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is True
    assert probe.await_count == 3


@pytest.mark.asyncio
async def test_single_pass_does_not_resolve() -> None:
    """T,F,T 처럼 연속이 끊기면 성공으로 보지 않는다."""
    with (
        patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        probe = AsyncMock(side_effect=[True, False, True])
        ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is False
    assert probe.await_count == 3


@pytest.mark.asyncio
async def test_verification_gives_up_after_attempt_budget() -> None:
    with (
        patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        probe = AsyncMock(return_value=True)
        probe.side_effect = [False, True, False]
        ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_verification_treats_probe_exception_as_failure() -> None:
    with (
        patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        probe = AsyncMock(side_effect=[True, RuntimeError("probe boom"), True])
        ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_no_probe_keeps_exit_code_semantics() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        ok = await SystemdRecoveryAdapter().execute_recovery(
            RecoveryAction("svc:x", "service", "down"))
    assert ok is True
