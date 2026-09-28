#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/test_research_duckai.py — pytest (unit + gated live)
"""Duck.ai warm-session client tests.

Unit tests cover the pure helpers (SSE parsing, error mapping, proxy env/auth).
The live browser test is opt-in: DEVFORGE_DUCKAI_LIVE=1 (needs Chromium + proxy).
"""

from __future__ import annotations

import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research import duckai  # noqa: E402

# ── _parse_sse ────────────────────────────────────────────────────────


def test_should_join_message_chunks_when_stream_has_multiple_events():
    body = 'data: {"message":"서"}\n\ndata: {"message":"울"}\n\ndata: [DONE]\n\n'
    assert duckai._parse_sse(body) == "서울"


def test_should_ignore_malformed_events_when_stream_has_noise():
    body = 'data: {not json}\n\ndata: {"message":"ok"}\n\n'
    assert duckai._parse_sse(body) == "ok"


def test_should_raise_when_stream_has_no_message_chunks():
    with pytest.raises(duckai.DuckAIError, match="empty answer"):
        duckai._parse_sse("data: [DONE]\n\n")


# ── _describe_error ───────────────────────────────────────────────────


def test_should_report_type_and_code_when_bn_limit_error():
    msg = duckai._describe_error(418, '{"type":"ERR_BN_LIMIT","overrideCode":"84f2"}')
    assert "ERR_BN_LIMIT" in msg
    assert "84f2" in msg
    assert "headed" in msg


def test_should_fall_back_to_raw_body_when_error_is_not_json():
    msg = duckai._describe_error(502, "<html>bad gateway</html>")
    assert "502" in msg
    assert "bad gateway" in msg


# ── _build_proxy_config ───────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["DATAIMPULSE_USER", "DATAIMPULSE_PASS", "DATAIMPULSE_PORT"])
def test_should_return_no_proxy_when_credentials_incomplete(monkeypatch, missing):
    monkeypatch.setenv("DATAIMPULSE_USER", "u")
    monkeypatch.setenv("DATAIMPULSE_PASS", "p")
    monkeypatch.setenv("DATAIMPULSE_HOST", "h.example.com")
    monkeypatch.setenv("DATAIMPULSE_PORT", "823")
    monkeypatch.delenv(missing, raising=False)
    assert duckai._build_proxy_config() is None


def test_should_build_local_proxy_url_when_credentials_present(monkeypatch):
    monkeypatch.setenv("DATAIMPULSE_USER", "u")
    monkeypatch.setenv("DATAIMPULSE_PASS", "p")
    monkeypatch.setenv("DATAIMPULSE_HOST", "127.0.0.1")
    monkeypatch.setenv("DATAIMPULSE_PORT", "9")
    cfg = duckai._build_proxy_config()
    assert cfg is not None
    assert cfg["server"].startswith("http://127.0.0.1:")


# ── auth header injection ─────────────────────────────────────────────


def test_should_add_authorization_when_header_absent():
    head = b"CONNECT duck.ai:443 HTTP/1.1\r\nHost: duck.ai:443"
    out = duckai._inject_proxy_auth(head, "Basic abc")
    assert out.endswith(b"Proxy-Authorization: Basic abc")


def test_should_replace_authorization_when_header_present():
    head = b"CONNECT duck.ai:443 HTTP/1.1\r\nProxy-Authorization: Basic old\r\nHost: duck.ai:443"
    out = duckai._inject_proxy_auth(head, "Basic new")
    assert b"Basic old" not in out
    assert out.count(b"Proxy-Authorization:") == 1
    assert b"Basic new" in out


def test_should_fallback_when_env_float_is_invalid(monkeypatch):
    monkeypatch.setenv("DEVFORGE_DUCKAI_TEST_FLOAT", "not-a-number")
    assert duckai._env_float("DEVFORGE_DUCKAI_TEST_FLOAT", 7.5) == 7.5
    monkeypatch.setenv("DEVFORGE_DUCKAI_TEST_FLOAT", "3")
    assert duckai._env_float("DEVFORGE_DUCKAI_TEST_FLOAT", 7.5) == 3.0


# ── _user_data_dir ────────────────────────────────────────────────────


def test_should_use_cache_env_when_set(monkeypatch, tmp_path):
    target = tmp_path / "profile"
    monkeypatch.setenv("DEVFORGE_DUCKAI_CACHE_DIR", str(target))
    assert duckai._user_data_dir() == str(target)
    assert target.is_dir()


def test_should_default_to_home_cache_when_env_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("DEVFORGE_DUCKAI_CACHE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    path = duckai._user_data_dir()
    assert path == str(tmp_path / ".cache" / "devforge" / "duckai")
    assert os.path.isdir(path)


def test_should_treat_gpt_prefix_as_same_when_normalizing_model():
    assert duckai._normalize_model("GPT-5.6 Luna") == duckai._normalize_model("5.6 Luna")
    assert duckai._normalize_model("GPT-5.4 mini") == duckai._normalize_model("5.4 mini")
    assert duckai._normalize_model("GPT-5.6 Luna") != duckai._normalize_model("Mistral Small 4")


# ── _decode_body ──────────────────────────────────────────────────────


def test_should_recover_utf8_when_body_is_windows1252_mojibake():
    original = 'data: {"message":"서울"}\n\n'
    mangled = original.encode("utf-8").decode("cp1252").encode("utf-8")
    assert duckai._decode_body(mangled) == original


def test_should_keep_body_when_already_valid_utf8():
    original = 'data: {"message":"서울"}\n\n'
    assert duckai._decode_body(original.encode("utf-8")) == original


def test_should_keep_ascii_when_body_has_no_high_bytes():
    body = b'data: {"message":"OK"}\n\n'
    assert duckai._decode_body(body) == 'data: {"message":"OK"}\n\n'


# ── live (opt-in) ─────────────────────────────────────────────────────


@pytest.mark.integration
def test_should_answer_when_live_session_available():
    if os.environ.get("DEVFORGE_DUCKAI_LIVE") != "1":
        pytest.skip("set DEVFORGE_DUCKAI_LIVE=1 to run the live Duck.ai test")
    client = duckai.DuckAIClient()
    try:
        answer = client.ask("대한민국의 수도는? 한 단어로만 답해줘.")
    finally:
        client.close()
    assert "서울" in answer
