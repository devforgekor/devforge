#!/usr/bin/env python3
# Status: experimental
# Path: application/watchdog_service.py, cli.py
"""Slack notification adapter (async via httpx)."""
from __future__ import annotations

from typing import Any

import httpx

from devforge.domain.watchdog.model import ComponentState, ComponentStatus, RecoveryAction
from devforge.ports.notification import NotificationPort


class SlackNotifier(NotificationPort):
    """Send notifications to Slack."""

    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send_alert(self, component: str, status: ComponentStatus) -> bool:
        """Send alert for component failure."""
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
                            f"Circuit: {'🔴 OPEN' if status.circuit_open else '🟢 CLOSED'}"
                        ),
                    },
                }
            ],
        }

        return await self._send(message)

    async def send_recovery(
        self, component: str, action: RecoveryAction, success: bool
    ) -> bool:
        """Send recovery notification."""
        icon = "✅" if success else "❌"
        message: dict[str, Any] = {
            "text": f"{icon} Recovery: {component}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{icon} *Recovery: {component}*\n"
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
            ComponentState.HEALTHY: "🟢",
            ComponentState.DEGRADED: "🟡",
            ComponentState.CRITICAL: "🔴",
            ComponentState.RECOVERING: "🔵",
        }
        return mapping.get(state, "⚪")

    async def _send(self, message: dict[str, Any]) -> bool:
        """Send message to Slack webhook."""
        try:
            response = await httpx.post(  # type: ignore[misc]
                self._webhook_url,
                json=message,
                timeout=10,
            )
            return bool(response.status_code == 200)
        except Exception:
            return False
