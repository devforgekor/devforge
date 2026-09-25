#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: memory adapter vs `free -m` (always runnable on Linux)."""
from __future__ import annotations

import subprocess

import pytest

from devforge.adapters.driven.health.system_health import (
    MEM_CRIT_PCT,
    MEM_WARN_PCT,
    SWAP_CRIT_MB,
    MemoryHealthChecker,
)

pytestmark = pytest.mark.characterization

legacy = pytest.importorskip("lib.watchdog.config")


def test_memory_thresholds_match_legacy() -> None:
    # [WHY] 임계치가 다르면 오탐이 난다 — 2026-09-25 shadow 창에서 swap 1550MB로
    # 74회 오탐(legacy는 all_ok 판정)했다. legacy=SSOT.
    assert MEM_CRIT_PCT == legacy.MEM_CRIT_PCT
    assert SWAP_CRIT_MB == legacy.SWAP_CRIT_MB
    assert MEM_WARN_PCT == legacy.MEM_WARN_PCT


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
