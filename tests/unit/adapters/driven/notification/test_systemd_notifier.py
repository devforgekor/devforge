#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/notification/
"""Tests for systemd sd_notify notifier (D2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.notification.systemd_notifier import SystemdNotifier


@pytest.mark.asyncio
async def test_sd_notify_ok() -> None:
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        assert await SystemdNotifier().sd_notify("STATUS=ready") is True


@pytest.mark.asyncio
async def test_send_alert_builds_status() -> None:
    calls: list[list[str]] = []

    async def fake(func, cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return MagicMock(returncode=0)

    with patch("asyncio.to_thread", new=fake):
        ok = await SystemdNotifier().send_alert("svc:x", "DOWN", "inactive")
    assert ok and "STATUS=ALERT svc:x DOWN inactive" in calls[0]
