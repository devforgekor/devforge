#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/test_notify.py — scripts/lib/notify.py
"""Telegram 알림 발신 키 분리 회귀 테스트 (lib/notify.py).

[WHY] 알림 봇(@Devforge_ping_bot)과 대화 봇을 분리한 뒤 발신 키가
`TELEGRAM_ALERT_TOKEN_KEY` 하나로 고정됐다. 채팅 id는 봇과 무관하므로
`TELEGRAM_CHAT_ID` 를 그대로 쓴다. 토큰 값은 절대 assert 하지 않는다
(AGENTS.md §0).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

NOTIFY = Path(__file__).resolve().parents[2] / "scripts" / "lib" / "notify.py"


@pytest.fixture(scope="module")
def notify():
    spec = importlib.util.spec_from_file_location("devforge_notify", NOTIFY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _telegram_targets(notifier):
    return [s for s in notifier._apprise.servers if s.url(privacy=True).startswith("tgram://")]


def test_should_register_telegram_target_when_alert_key_present(notify):
    secrets = {"TELEGRAM_ALERT_TOKEN_KEY": "123456:dummy-token", "TELEGRAM_CHAT_ID": "8278199280"}

    targets = _telegram_targets(notify.Notifier(secrets))

    assert len(targets) == 1
    assert targets[0].url(privacy=True).startswith("tgram://")


def test_should_not_register_telegram_target_when_alert_key_missing(notify):
    secrets = {"TELEGRAM_CHAT_ID": "8278199280"}

    assert _telegram_targets(notify.Notifier(secrets)) == []


def test_should_not_register_telegram_target_when_chat_id_missing(notify):
    secrets = {"TELEGRAM_ALERT_TOKEN_KEY": "123456:dummy-token"}

    assert _telegram_targets(notify.Notifier(secrets)) == []


# ── 중복 억제 (ALERT_DEDUP_SEC) ──────────────────────────────────────


@pytest.fixture
def notifier_with_recorder(notify, monkeypatch, tmp_path):
    """dedup 상태를 tmp 에 두고 발송만 기록하는 Notifier."""
    monkeypatch.setattr(notify, "DEDUP_FILE", tmp_path / "notify-dedup.json")
    sent: list[dict] = []
    n = notify.Notifier(
        {"TELEGRAM_ALERT_TOKEN_KEY": "123456:dummy-token", "TELEGRAM_CHAT_ID": "8278199280"}
    )
    monkeypatch.setattr(n._apprise, "notify", lambda **kwargs: sent.append(kwargs) or True)
    return n, sent


def test_should_send_telegram_once_when_duplicate_within_window(notifier_with_recorder):
    notifier, sent = notifier_with_recorder

    assert notifier.send_telegram("same alert") is True
    assert notifier.send_telegram("same alert") is True  # 중복은 '성공'으로 보고

    assert len(sent) == 1


def test_should_send_telegram_when_text_differs(notifier_with_recorder):
    notifier, sent = notifier_with_recorder

    notifier.send_telegram("alert one")
    notifier.send_telegram("alert two")

    assert len(sent) == 2


def test_should_send_telegram_again_after_window(notifier_with_recorder, notify):
    notifier, sent = notifier_with_recorder

    notifier.send_telegram("same alert")
    notify._dedup_record(notify._fingerprint("", "same alert"))
    # 창이 지난 상태로 되감는다
    state = notify._read_dedup_state()
    for key in state:
        state[key] -= notify.ALERT_DEDUP_SEC
    notify._write_dedup_state(state)

    notifier.send_telegram("same alert")

    assert len(sent) == 2


def test_should_allow_retry_when_send_failed(notifier_with_recorder, monkeypatch):
    notifier, sent = notifier_with_recorder
    monkeypatch.setattr(notifier._apprise, "notify", lambda **kwargs: sent.append(kwargs) or False)

    assert notifier.send_telegram("failing alert") is False
    assert notifier.send_telegram("failing alert") is False

    assert len(sent) == 2  # 실패는 기록되지 않아 재시도가능
