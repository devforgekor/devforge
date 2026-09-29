#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/adapters/driven/notification/
"""Tests for Telegram notifier (알림 채널 Telegram 통일)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.notification.telegram_notifier import TelegramNotifier

TOKEN = "123456:secret-token-should-not-be-logged"


def _client(ok: bool = True):  # type: ignore[no-untyped-def]
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.post = AsyncMock(return_value=MagicMock(json=lambda: {"ok": ok}, text=""))
    return c


@pytest.mark.asyncio
async def test_send_alert_ok() -> None:
    with patch("httpx.AsyncClient", return_value=_client()):
        sent = await TelegramNotifier(TOKEN, "42").send_alert("svc:x", "UNHEALTHY", "inactive")
    assert sent is True


@pytest.mark.asyncio
async def test_send_alert_api_error() -> None:
    with patch("httpx.AsyncClient", return_value=_client(ok=False)):
        sent = await TelegramNotifier(TOKEN, "42").send_alert("svc:x", "DOWN", "down")
    assert sent is False


@pytest.mark.asyncio
async def test_send_recovery_ok() -> None:
    with patch("httpx.AsyncClient", return_value=_client()):
        sent = await TelegramNotifier(TOKEN, "42").send_recovery("svc:x", "restarted")
    assert sent is True


@pytest.mark.asyncio
async def test_network_error_returns_false() -> None:
    c = _client()
    c.post = AsyncMock(side_effect=RuntimeError("no net"))
    with patch("httpx.AsyncClient", return_value=c):
        sent = await TelegramNotifier(TOKEN, "42").send_alert("svc:x", "DOWN", "d")
    assert sent is False


def test_from_env_returns_none_when_credentials_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_ALERT_TOKEN_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramNotifier.from_env() is None


def test_from_env_returns_notifier_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_ALERT_TOKEN_KEY", "tok")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    notifier = TelegramNotifier.from_env()
    assert notifier is not None
    assert notifier._chat_id == "42"


@pytest.mark.asyncio
async def test_send_should_suppress_httpx_logging_but_restore_level() -> None:
    """[WHY] httpx INFO 를 끄지 않으면 토큰이 URL 경로 그대로 저널에 남는다."""
    httpx_log = logging.getLogger("httpx")
    httpx_log.setLevel(logging.INFO)
    client = _client()
    seen: dict[str, int] = {}

    def _post(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen["level"] = httpx_log.level
        return MagicMock(json=lambda: {"ok": True})

    client.post = MagicMock(side_effect=_post)
    with patch("httpx.AsyncClient", return_value=client):
        await TelegramNotifier(TOKEN, "42").send_alert("svc:x", "DOWN", "d")

    assert seen["level"] == logging.WARNING  # 요청 중에는 httpx 로그가 눌려있음
    assert httpx_log.level == logging.INFO  # 호출 후 원래 레벨로 복원
