#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/notification/
"""Tests for Slack notifier (D1)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.notification.slack_notifier import SlackNotifier


def _client(json_ok: bool = True):  # type: ignore[no-untyped-def]
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.post = AsyncMock(return_value=MagicMock(json=lambda: {"ok": json_ok}, text=""))
    return c


@pytest.mark.asyncio
async def test_send_alert_ok() -> None:
    with patch("httpx.AsyncClient", return_value=_client()):
        assert await SlackNotifier("tok", "#chan").send_alert("svc:x", "UNHEALTHY", "inactive") is True


@pytest.mark.asyncio
async def test_send_alert_api_error() -> None:
    with patch("httpx.AsyncClient", return_value=_client(json_ok=False)):
        assert await SlackNotifier("tok", "#chan").send_alert("svc:x", "DOWN", "down") is False


@pytest.mark.asyncio
async def test_send_recovery_ok() -> None:
    with patch("httpx.AsyncClient", return_value=_client()):
        assert await SlackNotifier("tok", "#chan").send_recovery("svc:x", "restarted") is True


@pytest.mark.asyncio
async def test_network_error_returns_false() -> None:
    c = _client()
    c.post = AsyncMock(side_effect=RuntimeError("no net"))
    with patch("httpx.AsyncClient", return_value=c):
        assert await SlackNotifier("tok", "#chan").send_alert("svc:x", "DOWN", "d") is False
