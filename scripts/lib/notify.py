#!/usr/bin/env python3.11
# Status: experimental
# Path: lib/notify.py — imported by app.py, telegram_send.py, mcp_server.py
"""Unified notification module — Apprise (Telegram, Email) + native Slack.

Usage:
    from lib.notify import Notifier
    n = Notifier(secrets_dict)
    n.send_all("Title", "Body")       # Telegram + Email
    n.send_telegram("Message")         # Telegram only
    n.send_slack("C123", "Text")       # Slack channel/user DM
"""

import hashlib
import json
import logging
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

import apprise

logger = logging.getLogger("devforge-notify")

# ── 중복 억제 ────────────────────────────────────────────────────────
# [WHY] 같은 내용의 알림이 재시도/루프로 반복되면 알림 자체가 무의미해진다.
# 업계 표준(Prometheus Alertmanager group_interval, PagerDuty auto-pause)은
# "창 안의 동일 알림은 흡수"하는 방식이라 여기서도 동일 지문만 흡수한다.
ALERT_DEDUP_SEC = 600
DEDUP_FILE = Path.home() / ".config/devforge/notify-dedup.json"
DEDUP_MAX_ENTRIES = 200


def _fingerprint(*parts: str) -> str:
    """중복 판정용 지문. 내용이 같으면 같은 알림으로 본다."""
    joined = "\n".join(part.strip() for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _read_dedup_state() -> dict[str, float]:
    try:
        raw = json.loads(DEDUP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def _write_dedup_state(state: dict[str, float]) -> None:
    try:
        DEDUP_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = DEDUP_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, DEDUP_FILE)
    except OSError as exc:  # 상태 실패가 발송을 막지 않는다
        logger.debug("notify: dedup state write failed: %s", type(exc).__name__)


def _dedup_check(key: str) -> bool:
    """창 안의 동일 지문이 아니면 True(발송 진행). 기록은 발송 성공 후에 한다."""
    last = _read_dedup_state().get(key)
    if last is not None and time.time() - last < ALERT_DEDUP_SEC:
        logger.info("notify: duplicate suppressed (window=%ds)", ALERT_DEDUP_SEC)
        return False
    return True


def _dedup_record(key: str) -> None:
    state = _read_dedup_state()
    state[key] = time.time()
    if len(state) > DEDUP_MAX_ENTRIES:
        for stale in sorted(state, key=state.get)[: len(state) - DEDUP_MAX_ENTRIES]:
            state.pop(stale, None)
    _write_dedup_state(state)


class Notifier:
    """Apprise-based notification hub.

    Telegram and Email use Apprise (unified, extensible).
    Slack uses native API because Apprise's slack plugin has a bug:
    it passes @user_id to Slack API as-is (e.g. "@U123") instead of
    stripping the "@" prefix, which causes channel_not_found.
    The "#channel" and "+encoded_id" modes work fine, but we need DM
    for interactive confirm/reject buttons, so native API it is.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets
        self._apprise = self._build_apprise()

    # ── Apprise (Telegram + Email) ──────────────────────────────

    def _build_apprise(self) -> apprise.Apprise:
        a_obj = apprise.Apprise()

        # Telegram — 알림 전용 봇(@Devforge_ping_bot).
        # [WHY] 대화 봇과 알림 봇을 분리해 알림 소음이 대화 세션을 간섭하지 않는다.
        # 발신 토큰은 TELEGRAM_ALERT_TOKEN_KEY 하나만 쓴다. 채팅 id는 봇과
        # 무관하므로 TELEGRAM-CHAT-ID(내 DM id)를 그대로 쓴다.
        tg_token = self._secrets.get("TELEGRAM_ALERT_TOKEN_KEY", "")
        tg_chat = self._secrets.get("TELEGRAM_CHAT_ID", "")
        if tg_token and tg_chat:
            a_obj.add(f"tgram://{tg_token}/{tg_chat}")
            logger.info("notify: Telegram loaded")
        else:
            logger.warning(
                "notify: Telegram skipped (TELEGRAM_ALERT_TOKEN_KEY or TELEGRAM_CHAT_ID missing)"
            )

        # Email (Gmail SMTP via SSL)
        smtp_user = self._secrets.get("GMAIL_SMTP_USER_MINIPARK4U", "") or self._secrets.get("SMTP_USER", "")
        smtp_pass = self._secrets.get("GMAIL_SMTP_PASSWORD_MINIPARK4U", "").replace(" ", "%20")
        if smtp_user and smtp_pass:
            a_obj.add(
                f"mailto://{smtp_user}:{smtp_pass}@smtp.gmail.com:465?from={smtp_user}&mode=ssl"
            )
            logger.info("notify: Email loaded")

        return a_obj

    def send_all(self, title: str, body: str, body_format: int = apprise.NotifyFormat.TEXT) -> bool:
        """Send to all Apprise-configured channels (Telegram + Email).

        동일(title+body) 알림은 ALERT_DEDUP_SEC 창에서 1회만 발송한다.
        """
        key = _fingerprint(title, body)
        if not _dedup_check(key):
            return True  # [WHY] 실패로 보고하면 호출부가 재시도해 소음이 커진다
        sent = self._apprise.notify(title=title, body=body, body_format=body_format)
        if sent:
            _dedup_record(key)
        return sent

    def send_telegram(self, text: str) -> bool:
        """Send a text message via Telegram only.

        동일 내용은 ALERT_DEDUP_SEC 창에서 1회만 발송한다.
        """
        key = _fingerprint("", text)
        if not _dedup_check(key):
            logger.info("notify: Telegram duplicate suppressed")
            return True
        sent = self._apprise.notify(title="", body=text)
        if sent:
            _dedup_record(key)
        return sent

    # ── Slack (native) ──────────────────────────────────────────

    def send_slack(self, channel: str, text_or_data: Any) -> bool:
        """Send a Slack message via chat.postMessage.

        Args:
            channel: Slack channel/user ID or "chat.update" for update.
            text_or_data: Plain text string or dict payload for chat.update.
        """
        bot_token = self._secrets.get("SLACK_BOT_TOKEN_KEY", "")
        if not bot_token:
            logger.error("No SLACK_BOT_TOKEN_KEY, cannot send Slack")
            return False

        if isinstance(text_or_data, str):
            payload_data = {"channel": channel, "text": text_or_data, "mrkdwn": True}
        else:
            payload_data = text_or_data

        payload = json.dumps(payload_data).encode()
        api_url = (
            "https://slack.com/api/chat.update"
            if channel == "chat.update"
            else "https://slack.com/api/chat.postMessage"
        )
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read())
                if not result.get("ok"):
                    logger.error("Slack API error: %s", result.get("error", "?"))
                    return False
                return True
        except Exception as e:
            logger.error("Slack post failed: %s", e)
            return False

    # ── Convenience ─────────────────────────────────────────────

    def slack_post(self, channel: str, text: str) -> bool:
        """Alias: send plain text to a Slack channel."""
        return self.send_slack(channel, text)

    def slack_update(self, channel: str, ts: str, text: str, blocks: list) -> bool:
        """Update a Slack message (chat.update)."""
        return self.send_slack(
            "chat.update",
            {
                "channel": channel,
                "ts": ts,
                "text": text,
                "blocks": blocks,
            },
        )
