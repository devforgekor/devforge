#!/usr/bin/env python3.12
# Status: experimental
# Path: adapters/driven/notification/
"""Telegram bot notifier (알림 전용 봇 @Devforge_ping_bot).

[WHY] Slack 봇 토큰이 account_inactive 로 죽어 24시간 동안 알림이 전량
소실됐다. 알림 채널을 Telegram 하나로 통일하고, Slack 은 코드를 남기되
연결만 해제한다(WATCHDOG_SLACK_ENABLED=1 로 재연결).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from devforge.ports.notification import NotificationPort

log = logging.getLogger(__name__)
TELEGRAM_API = "https://api.telegram.org/bot"


class TelegramNotifier(NotificationPort):
    def __init__(self, token: str, chat_id: str, timeout: int = 10) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    @classmethod
    def from_env(cls) -> Optional["TelegramNotifier"]:
        """환경변수에 알림 토큰/채팅 id 가 있으면 생성, 없으면 None."""
        token = os.environ.get("TELEGRAM_ALERT_TOKEN_KEY", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return None
        return cls(token, chat_id)

    async def _send(self, text: str) -> bool:
        # [WHY] httpx 는 INFO 에서 요청 URL 전체를 남긴다. Telegram 토큰은
        # URL 경로에 들어가므로 로그에 그대로 유출된다(AGENTS.md §0).
        httpx_log = logging.getLogger("httpx")
        previous_level = httpx_log.level
        httpx_log.setLevel(logging.WARNING)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(
                    f"{TELEGRAM_API}{self._token}/sendMessage",
                    json={"chat_id": self._chat_id, "text": text},
                )
                payload = r.json()
                ok = bool(payload.get("ok"))
                if not ok:
                    log.warning("telegram sendMessage not ok: %s", payload.get("error_code"))
                return ok
        except Exception as e:  # noqa: BLE001
            log.warning("telegram sendMessage failed: %s", type(e).__name__)
            return False
        finally:
            httpx_log.setLevel(previous_level)

    async def send_alert(self, component: str, state: str, detail: str) -> bool:
        return await self._send(f"🚨 {component} → {state}: {detail}")

    async def send_recovery(self, component: str, detail: str) -> bool:
        return await self._send(f"✅ {component} recovered: {detail}")

    async def sd_notify(self, state: str) -> bool:
        return False  # Telegram does not implement sd_notify
