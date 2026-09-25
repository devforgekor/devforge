#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for LLM health adapter (C2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.health.llm_health import LLMHealthChecker


def _client(get_status: int = 200, post_status: int = 200, post_json=None,
            get_exc=None, post_exc=None):  # type: ignore[no-untyped-def]
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.get = AsyncMock(side_effect=get_exc, return_value=MagicMock(status_code=get_status))
    if post_json is None:
        post_json = {"choices": [{"message": {"content": ""}}]}
    c.post = AsyncMock(side_effect=post_exc,
                       return_value=MagicMock(status_code=post_status, json=lambda: post_json))
    return c


@pytest.mark.asyncio
async def test_probe_ok() -> None:
    with patch("httpx.AsyncClient", return_value=_client()):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy and checks[0].component == "llm:day-extract"


@pytest.mark.asyncio
async def test_t1_failure() -> None:
    with patch("httpx.AsyncClient", return_value=_client(get_status=500)):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_transient_503_is_healthy() -> None:
    with patch("httpx.AsyncClient", return_value=_client(get_exc=Exception("HTTP 503 loading model"))):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is True and "transient" in checks[0].detail


@pytest.mark.asyncio
async def test_night_port_skipped_in_day() -> None:
    c = _client()
    with patch("httpx.AsyncClient", return_value=c):
        checks = await LLMHealthChecker({"night-verify": 8084}, day_ports={8082}).check_health()
    assert checks == []


@pytest.mark.asyncio
async def test_connection_refused_unhealthy() -> None:
    with patch("httpx.AsyncClient", return_value=_client(get_exc=Exception("connection refused"))):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_probe_sets_latency_metric() -> None:
    with patch("httpx.AsyncClient", return_value=_client()):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].metric_value is not None
    assert checks[0].threshold == 6000.0  # 2000ms baseline x3


@pytest.mark.asyncio
async def test_non_serving_port_skipped() -> None:
    # legacy check_all_llm(): 현재 서빙 포트만 프로브한다(비서빙 포트 오탐 방지).
    client = _client()
    with patch("httpx.AsyncClient", return_value=client):
        checks = await LLMHealthChecker(
            {"day-extract": 8082, "night-verify": 8084},
            day_ports={8082, 8084},
            serving_port_reader=lambda: 8082,
        ).check_health()
    assert [c.component for c in checks] == ["llm:day-extract"]
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_empty_exception_message_reports_type_name() -> None:
    # httpx.ReadError('')처럼 메시지가 빈 예외는 detail이 공란이 되지 않아야 한다.
    with patch("httpx.AsyncClient", return_value=_client(get_exc=Exception(""))):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is False
    assert checks[0].detail == "Exception"


def test_runtime_mode_and_serving_port_read_from_env_file(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from devforge.adapters.driven.health import llm_health

    sysfile = tmp_path / "current-system-mode.env"
    sysfile.write_text("MODE=night\n")
    rtfile = tmp_path / "current-mode-inference.env"
    rtfile.write_text("MODE=night\nPORT=8084\n")
    monkeypatch.setattr(llm_health, "SYSTEM_ENV_FILE", sysfile)
    monkeypatch.setattr(llm_health, "RUNTIME_ENV_FILE", rtfile)
    assert llm_health.read_runtime_mode() == "night"
    assert llm_health.read_serving_port() == 8084


def test_runtime_readers_fall_back_when_file_missing(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from devforge.adapters.driven.health import llm_health

    monkeypatch.setattr(llm_health, "SYSTEM_ENV_FILE", tmp_path / "nope.env")
    monkeypatch.setattr(llm_health, "RUNTIME_ENV_FILE", tmp_path / "nope.env")
    assert llm_health.read_runtime_mode() == "day"
    assert llm_health.read_serving_port() is None
