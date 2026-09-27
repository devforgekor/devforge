#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/test_research_proxy.py — pytest (unit)
"""DataImpulse proxy helper tests (lib/research/proxy.py)."""

from __future__ import annotations

import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research import proxy  # noqa: E402

_ENV_KEYS = (
    "DATAIMPULSE_USER",
    "DATAIMPULSE_PASS",
    "DATAIMPULSE_HOST",
    "DATAIMPULSE_PORT",
    "DATAIMPULSE_API_KEY",
    "DATAIMPULSE_LOGIN",
    "DATAIMPULSE_PROXY_KEY",
    proxy.COUNTRY_ENV,
)


@pytest.fixture
def clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_should_parse_combined_key_when_userinfo_at_host_format():
    assert proxy.parse_proxy_key("u1:p1@gw.example.com:823") == (
        "u1",
        "p1",
        "gw.example.com",
        "823",
    )


def test_should_map_api_key_to_user_when_user_absent(clean_env):
    clean_env.setenv("DATAIMPULSE_API_KEY", "acct")
    clean_env.setenv("DATAIMPULSE_PASS", "pw")
    assert proxy.load_proxy_env()["DATAIMPULSE_USER"] == "acct"


def test_should_fill_fields_when_only_combined_key_present(clean_env):
    clean_env.setenv("DATAIMPULSE_PROXY_KEY", "u:p@h.example.com:9999")
    env = proxy.load_proxy_env()
    assert (env["DATAIMPULSE_USER"], env["DATAIMPULSE_PASS"]) == ("u", "p")
    assert (env["DATAIMPULSE_HOST"], env["DATAIMPULSE_PORT"]) == ("h.example.com", "9999")


def test_should_default_country_to_kr_when_env_unset(clean_env):
    assert proxy.proxy_country() == "kr"


def test_should_lowercase_country_when_env_set(clean_env):
    clean_env.setenv(proxy.COUNTRY_ENV, "US")
    assert proxy.proxy_country() == "us"


@pytest.mark.parametrize("missing", ["DATAIMPULSE_USER", "DATAIMPULSE_PASS", "DATAIMPULSE_PORT"])
def test_should_return_none_credentials_when_incomplete(clean_env, missing):
    clean_env.setenv("DATAIMPULSE_USER", "u")
    clean_env.setenv("DATAIMPULSE_PASS", "p")
    clean_env.setenv("DATAIMPULSE_HOST", "h.example.com")
    clean_env.setenv("DATAIMPULSE_PORT", "823")
    clean_env.delenv(missing, raising=False)
    assert proxy.credentials() is None
    assert proxy.proxy_url() is None


def test_should_target_country_when_user_has_no_suffix(clean_env):
    clean_env.setenv("DATAIMPULSE_USER", "u")
    clean_env.setenv("DATAIMPULSE_PASS", "p")
    clean_env.setenv("DATAIMPULSE_PORT", "823")
    clean_env.setenv(proxy.COUNTRY_ENV, "kr")
    user, _password, host, _port = proxy.credentials()
    assert user == "u__cr.kr"
    assert host == proxy.DEFAULT_HOST


def test_should_not_double_append_country_when_already_targeted(clean_env):
    clean_env.setenv("DATAIMPULSE_USER", "u__cr.us")
    clean_env.setenv("DATAIMPULSE_PASS", "p")
    clean_env.setenv("DATAIMPULSE_PORT", "823")
    user, _password, _host, _port = proxy.credentials()
    assert user == "u__cr.us"


def test_should_build_url_when_credentials_present(clean_env):
    clean_env.setenv("DATAIMPULSE_USER", "u")
    clean_env.setenv("DATAIMPULSE_PASS", "p")
    clean_env.setenv("DATAIMPULSE_HOST", "gw.example.com")
    clean_env.setenv("DATAIMPULSE_PORT", "823")
    clean_env.setenv(proxy.COUNTRY_ENV, "kr")
    assert proxy.proxy_url() == "http://u__cr.kr:p@gw.example.com:823"
