#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for heartbeat health adapter (C4) — fake repository port."""
from __future__ import annotations

import pytest

from devforge.adapters.driven.health.pipeline_health import HeartbeatHealthChecker
from devforge.ports.heartbeat import HeartbeatReading


class _FakeRepo:
    def __init__(self, readings: list[HeartbeatReading]) -> None:
        self._readings = readings

    async def list_heartbeats(self) -> list[HeartbeatReading]:
        return list(self._readings)


class _ErrorRepo:
    async def list_heartbeats(self) -> list[HeartbeatReading]:
        raise RuntimeError("db down")


@pytest.mark.asyncio
async def test_fresh_heartbeat() -> None:
    repo = _FakeRepo([HeartbeatReading("day_extract", "IN_PROGRESS", 100)])
    checks = await HeartbeatHealthChecker(repo, {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy and checks[0].component == "heartbeat:day_extract"


@pytest.mark.asyncio
async def test_stale_heartbeat() -> None:
    repo = _FakeRepo([HeartbeatReading("day_extract", "IN_PROGRESS", 3000)])
    checks = await HeartbeatHealthChecker(repo, {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_never_beat() -> None:
    checks = await HeartbeatHealthChecker(_FakeRepo([]), {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy is False and "never" in checks[0].detail


@pytest.mark.asyncio
async def test_resolved_counts_alive() -> None:
    repo = _FakeRepo([HeartbeatReading("news_collector", "RESOLVED", 99999)])
    checks = await HeartbeatHealthChecker(repo, {"news_collector": 1800}).check_health()
    assert checks[0].is_healthy is True


@pytest.mark.asyncio
async def test_db_error_unhealthy() -> None:
    checks = await HeartbeatHealthChecker(_ErrorRepo(), {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy is False and "query failed" in checks[0].detail
