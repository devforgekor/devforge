#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/notification/
"""Slack Web API notifier (legacy notifier.py:60-81, 345, 362)."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from devforge.ports.notification import NotificationPort

log = logging.getLogger(__name__)
SLACK_API = "https://slack.com/api"


class SlackNotifier(NotificationPort):
    def __init__(self, token: str, channel: str, timeout: int = 10) -> None:
        self._token = token
        self._channel = channel
        self._timeout = timeout

    async def _post(self, method: str, payload: dict[str, Any]) -> bool:
        payload.setdefault("channel", self._channel)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(f"{SLACK_API}/{method}", json=payload,
                                      headers={"Authorization": f"Bearer {self._token}"})
                ok = bool(r.json().get("ok"))
                if not ok:
                    log.warning("slack %s not ok: %s", method, r.text[:200])
                return ok
        except Exception as e:  # noqa: BLE001
            log.warning("slack %s failed: %s", method, e)
            return False

    async def send_alert(self, component: str, state: str, detail: str) -> bool:
        return await self._post("chat.postMessage", {
            "text": f":rotating_light: {component} → {state}: {detail}",
        })

    async def send_recovery(self, component: str, detail: str) -> bool:
        return await self._post("chat.postMessage", {
            "text": f":white_check_mark: {component} recovered: {detail}",
        })

    async def sd_notify(self, state: str) -> bool:
        return False  # Slack does not implement sd_notify
