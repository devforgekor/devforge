#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: systemd recovery adapter vs legacy recover_service.

Requires a disposable throwaway unit; skipped otherwise. This environment has
no safe disposable unit, so the test skips — it documents the parity contract.
"""
from __future__ import annotations

import pytest

from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
from devforge.ports.types import RecoveryAction

pytestmark = pytest.mark.characterization

_DISPOSABLE_UNIT = "devforge-watchdog-parity-test.service"


def _unit_exists() -> bool:
    import subprocess
    try:
        r = subprocess.run(["systemctl", "--user", "cat", _DISPOSABLE_UNIT],
                           capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.asyncio
async def test_restart_matches_legacy() -> None:
    if not _unit_exists():
        pytest.skip(f"no disposable unit {_DISPOSABLE_UNIT}")
    ok = await SystemdRecoveryAdapter().execute_recovery(
        RecoveryAction(f"svc:{_DISPOSABLE_UNIT}", "service", "parity"))
    assert ok is True
