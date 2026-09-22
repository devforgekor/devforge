#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/notification/*, application/watchdog_service.py
"""Slack notification adapter."""
from __future__ import annotations

from typing import Any

import httpx

from devforge.domain.watchdog.model import ComponentState, ComponentStatus, RecoveryAction


class SlackNotifier:
    """Send notifications to Slack (async via httpx)."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send_alert(self, component: str, status: ComponentStatus) -> bool:
        emoji = self._state_emoji(status.state)
        message: dict[str, Any] = {
            "text": f"{emoji} *Alert: {component}*",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *Alert: {component}*\n"
                            f"State: {status.state.value}\n"
                            f"Failures: {status.consecutive_failures}\n"
                            f"Circuit: {'OPEN' if status.circuit_open else 'CLOSED'}"
                        ),
                    },
                }
            ],
        }
        return await self._send(message)

    async def send_recovery(
        self, component: str, action: RecoveryAction, success: bool
    ) -> bool:
        icon = "+" if success else "x"
        message: dict[str, Any] = {
            "text": f"[{icon}] Recovery: {component}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"[{icon}] *Recovery: {component}*\n"
                            f"Action: {action.action_type} (severity={action.severity})\n"
                            f"Result: {'SUCCESS' if success else 'FAILED'}\n"
                            f"Detail: {action.detail}"
                        ),
                    },
                }
            ],
        }
        return await self._send(message)

    def _state_emoji(self, state: ComponentState) -> str:
        mapping = {
            ComponentState.HEALTHY: "OK",
            ComponentState.DEGRADED: "WARN",
            ComponentState.CRITICAL: "CRIT",
            ComponentState.RECOVERING: "REC",
        }
        return mapping.get(state, "?")

    async def _send(self, message: dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._webhook_url, json=message)
                return resp.status_code == 200
        except (httpx.ConnectError, httpx.TimeoutException):
            return False
