#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: systemd health adapter vs live `systemctl --user is-active`."""
from __future__ import annotations

import subprocess

import pytest

from devforge.adapters.driven.health.systemd_health import SystemdServiceHealthChecker

pytestmark = pytest.mark.characterization

_UNIT = "devforge-turn-watcher"


def _unit_exists() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "cat", _UNIT],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.asyncio
async def test_service_state_matches_systemctl() -> None:
    if not _unit_exists():
        pytest.skip(f"{_UNIT} not present in this environment")
    live = subprocess.run(["systemctl", "--user", "is-active", _UNIT],
                          capture_output=True, text=True, timeout=5)
    expected = live.returncode == 0
    checks = await SystemdServiceHealthChecker([_UNIT]).check_health()
    assert checks[0].is_healthy is expected
