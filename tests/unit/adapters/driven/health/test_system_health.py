#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for memory/disk health adapters (C3)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.health.system_health import DiskHealthChecker, MemoryHealthChecker


@pytest.mark.asyncio
async def test_memory_ok() -> None:
    out = ("              total        used\n"
           "Mem:          16000        8000\n"
           "Swap:          4096         100\n")
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await MemoryHealthChecker().check_health()
    assert checks[0].is_healthy and checks[0].component == "system:memory"


@pytest.mark.asyncio
async def test_memory_crit() -> None:
    out = ("              total        used\n"
           "Mem:          16000       15800\n"
           "Swap:          4096        2048\n")
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await MemoryHealthChecker().check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_disk_ok() -> None:
    out = "Target  Use%\n/        40%\n/opt     50%\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await DiskHealthChecker(["/", "/opt"]).check_health()
    assert all(c.is_healthy for c in checks)
    assert {c.component for c in checks} == {"system:disk:/", "system:disk:/opt"}


@pytest.mark.asyncio
async def test_disk_high() -> None:
    out = "Target  Use%\n/        95%\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await DiskHealthChecker(["/"]).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_disk_mount_missing() -> None:
    out = "Target  Use%\n/        40%\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await DiskHealthChecker(["/opt/ai_data"]).check_health()
    assert checks[0].is_healthy is False and "not found" in checks[0].detail
