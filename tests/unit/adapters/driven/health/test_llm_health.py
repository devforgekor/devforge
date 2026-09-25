#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for LLM health adapter (C2)."""
from __future__ import annotations

import json
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


def _slots_client(busy, slots_exc=None, post_json=None):  # type: ignore[no-untyped-def]
    """GET /health·/slots을 URL별로 다르게 응답하는 클라이언트."""
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)

    async def _get(url, **kwargs):  # type: ignore[no-untyped-def]
        if slots_exc is not None and str(url).endswith("/slots"):
            raise slots_exc
        m = MagicMock()
        if str(url).endswith("/slots"):
            m.status_code = 200
            m.text = json.dumps([{"id": i, "is_processing": b} for i, b in enumerate(busy)])
        else:
            m.status_code = 200
        return m

    c.get = AsyncMock(side_effect=_get)
    if post_json is None:
        post_json = {"choices": [{"message": {"content": ""}}]}
    c.post = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: post_json))
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
    urls = [str(call.args[0]) for call in client.get.await_args_list]
    assert urls and all("8082" in u for u in urls)
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_all_slots_busy_reports_transient_healthy() -> None:
    # CPU 전용 llama.cpp가 긴 프롬프트로 두 슬롯을 점유하면 프로브는60s타임아웃을
    # 맞지만 서버는 정상 → 장애로 기록하지 않는다(포화 선감지, grace=0이면 즉시 판정).
    client = _slots_client([True, True])
    with patch("httpx.AsyncClient", return_value=client):
        checks = await LLMHealthChecker(
            {"day-extract": 8082}, saturation_grace_sec=0
        ).check_health()
    assert checks[0].is_healthy is True
    assert "transient" in checks[0].detail
    assert "2/2" in checks[0].detail
    client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_slot_still_runs_probe() -> None:
    client = _slots_client([True, False])
    with patch("httpx.AsyncClient", return_value=client):
        checks = await LLMHealthChecker(
            {"day-extract": 8082}, saturation_grace_sec=0
        ).check_health()
    assert checks[0].is_healthy is True
    assert checks[0].metric_value is not None
    client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_slots_endpoint_failure_falls_back_to_probe() -> None:
    client = _slots_client([], slots_exc=Exception("no /slots route"))
    with patch("httpx.AsyncClient", return_value=client):
        checks = await LLMHealthChecker(
            {"day-extract": 8082}, saturation_grace_sec=0
        ).check_health()
    assert checks[0].is_healthy is True
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_probe_runs_when_slot_frees_during_grace(
    monkeypatch,
) -> None:
    from devforge.adapters.driven.health import llm_health

    monkeypatch.setattr(llm_health, "SLOT_POLL_SEC", 0.01)
    client = _slots_client([True, True])
    real_get = client.get.side_effect
    slots_calls = {"n": 0}

    async def _get(url, **kwargs):  # type: ignore[no-untyped-def]
        resp = await real_get(url, **kwargs)
        if str(url).endswith("/slots"):
            slots_calls["n"] += 1
            if slots_calls["n"] >= 2:
                # 두 번째 /slots 호출에서 한 슬롯이 비는 시나리오
                resp.text = json.dumps(
                    [{"id": 0, "is_processing": True}, {"id": 1, "is_processing": False}]
                )
        return resp

    client.get = AsyncMock(side_effect=_get)
    with patch("httpx.AsyncClient", return_value=client):
        checks = await LLMHealthChecker(
            {"day-extract": 8082}, saturation_grace_sec=5
        ).check_health()
    assert checks[0].is_healthy is True
    assert checks[0].metric_value is not None  # 프로브가 실행됨
    client.post.assert_awaited_once()


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
