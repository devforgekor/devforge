#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: memory adapter vs `free -m` (always runnable on Linux)."""
from __future__ import annotations

import subprocess

import pytest

from devforge.adapters.driven.health.system_health import MemoryHealthChecker

pytestmark = pytest.mark.characterization


@pytest.mark.asyncio
async def test_memory_pct_matches_free() -> None:
    out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5).stdout
    pct = None
    for line in out.splitlines():
        if line.startswith("Mem:"):
            parts = line.split()
            pct = round(int(parts[2]) / int(parts[1]) * 100)
            break
    assert pct is not None
    checks = await MemoryHealthChecker().check_health()
    assert checks[0].metric_value is not None
    assert abs(checks[0].metric_value - pct) <= 2  # allow rounding/sampling drift
