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
