#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/test_openclaw_kv_resolver.py — scripts/deploy/openclaw-kv-resolver.py
"""OpenClaw exec SecretRef resolver: `NAME#path.to.key` 경로 추출 회귀 테스트.

[WHY] 평문 id 와 경로 id 가 한 resolver 에서 섞이므로, 경로 추가가 기존
gateway token / model key 해석을 깨뜨리지 않는지 고정한다. 실패 메시지가
토큰 값을 새지 않는지도 함께 고정한다(AGENTS.md §0).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

RESOLVER = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "openclaw-kv-resolver.py"

ALERT_TOKEN = "sk-live-dummy"
BOTS_JSON = (
    '{"bots": {"alert_bot": {"token": "%s", "username": "Devforge_clerk_bot"}}}' % ALERT_TOKEN
)


@pytest.fixture(scope="module")
def resolver():
    spec = importlib.util.spec_from_file_location("openclaw_kv_resolver", RESOLVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kv_stub(resolver, monkeypatch):
    """네트워크 없이 시크릿 값을 돌려주는 resolver."""
    secrets = {"TELEGRAM-ENV-CONFIG": BOTS_JSON, "PLAIN-KEY": "plain-value"}

    monkeypatch.setattr(resolver._kv, "get_token", lambda: "token")
    monkeypatch.setattr(resolver._kv, "KEYVAULT_URLS", ["https://example.vault.azure.net"])
    monkeypatch.setattr(
        resolver._kv, "get_secret_value", lambda token, url, name: secrets.get(name)
    )
    return secrets


def test_should_keep_name_only_when_path_absent(resolver):
    assert resolver.split_secret_id("OPENCODE-GATEWAY-TOKEN-KEY") == (
        "OPENCODE-GATEWAY-TOKEN-KEY",
        None,
    )


def test_should_split_name_and_path_when_path_present(resolver):
    assert resolver.split_secret_id("TELEGRAM-ENV-CONFIG#bots.alert_bot.token") == (
        "TELEGRAM-ENV-CONFIG",
        "bots.alert_bot.token",
    )


def test_should_reject_when_name_missing_after_separator(resolver):
    with pytest.raises(ValueError, match="시크릿 이름이 비어"):
        resolver.split_secret_id("#bots.alert_bot.token")


def test_should_reject_when_path_empty_after_separator(resolver):
    with pytest.raises(ValueError, match="경로가 비어"):
        resolver.split_secret_id("TELEGRAM-ENV-CONFIG#")


def test_should_extract_nested_string_when_path_valid(resolver):
    value = resolver.extract_json_path("TELEGRAM-ENV-CONFIG", BOTS_JSON, "bots.alert_bot.token")
    assert value == ALERT_TOKEN


def test_should_not_leak_value_when_path_missing(resolver):
    with pytest.raises(ValueError) as excinfo:
        resolver.extract_json_path("TELEGRAM-ENV-CONFIG", BOTS_JSON, "bots.nope.token")
    message = str(excinfo.value)
    assert "bots.nope" in message
    assert "sk-live-dummy" not in message


def test_should_reject_when_value_is_not_json(resolver):
    with pytest.raises(ValueError, match="JSON 이 아니"):
        resolver.extract_json_path("SECRET", "not-json", "bots.token")


def test_should_reject_when_value_is_json_but_not_object(resolver):
    with pytest.raises(ValueError, match="JSON 객체"):
        resolver.extract_json_path("SECRET", '["a"]', "bots.token")


def test_should_reject_when_leaf_is_not_string(resolver):
    with pytest.raises(ValueError, match="문자열이 아닙니다"):
        resolver.extract_json_path("SECRET", '{"a": 5}', "a")


def test_resolve_should_key_by_full_id_when_path_given(resolver, kv_stub):
    values, errors = resolver.resolve(["TELEGRAM-ENV-CONFIG#bots.alert_bot.token"])

    assert errors == {}
    assert values == {"TELEGRAM-ENV-CONFIG#bots.alert_bot.token": ALERT_TOKEN}


def test_resolve_should_keep_plain_id_behaviour(resolver, kv_stub):
    values, errors = resolver.resolve(["PLAIN-KEY"])

    assert errors == {}
    assert values == {"PLAIN-KEY": "plain-value"}


def test_resolve_should_report_error_keyed_by_full_id(resolver, kv_stub):
    values, errors = resolver.resolve(["TELEGRAM-ENV-CONFIG#bots.missing.token", "GONE-KEY"])

    assert values == {}
    assert set(errors) == {"TELEGRAM-ENV-CONFIG#bots.missing.token", "GONE-KEY"}
    assert "sk-live-dummy" not in errors["TELEGRAM-ENV-CONFIG#bots.missing.token"]
