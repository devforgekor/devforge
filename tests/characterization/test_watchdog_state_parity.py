#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: v2 ComponentTracker vs legacy lib.watchdog.state.ComponentTracker.

Pins: open@3, HALF_OPEN@120s, DOWN@5, reset@600s (Risk 3).
"""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.characterization

legacy_state = pytest.importorskip("lib.watchdog.state")

from devforge.domain.watchdog.monitoring.tracker import (  # noqa: E402
    CIRCUIT_BREAKER_TIMEOUT,
    ComponentTracker,
)


def test_open_at_three_and_timeout_constant() -> None:
    assert CIRCUIT_BREAKER_TIMEOUT == 120
    new = ComponentTracker("svc:x")
    old = legacy_state.ComponentTracker("svc:x")
    for _ in range(3):
        new.record_failure()
        old.record_failure()
    assert new.state.value == old.state.value == "UNHEALTHY"
    assert new.can_retry() is False and old.can_retry() is False


def test_down_at_five() -> None:
    new = ComponentTracker("svc:x")
    old = legacy_state.ComponentTracker("svc:x")
    for _ in range(5):
        new.record_failure()
        old.record_failure()
    assert new.state.value == old.state.value == "DOWN"


def test_degraded_then_healthy_reset() -> None:
    new = ComponentTracker("svc:x")
    old = legacy_state.ComponentTracker("svc:x")
    for _ in range(2):
        new.record_failure()
        old.record_failure()
    assert new.state.value == old.state.value == "DEGRADED"
    new.record_success()
    old.record_success()
    assert new.state.value == old.state.value == "HEALTHY"
    assert new.consecutive_fail == old.consecutive_fail == 0


def test_full_reset_after_backoff_reset_sec() -> None:
    new = ComponentTracker("svc:x")
    old = legacy_state.ComponentTracker("svc:x")
    for _ in range(2):
        new.record_failure()
        old.record_failure()
    new.last_fail_ts = time.monotonic() - 601
    old.last_fail_ts = time.monotonic() - 601
    new.record_success()
    old.record_success()
    assert new.fail_count == old.fail_count == 0


def test_half_open_after_timeout() -> None:
    new = ComponentTracker("svc:x")
    old = legacy_state.ComponentTracker("svc:x")
    for _ in range(3):
        new.record_failure()
        old.record_failure()
    new.circuit_open_until = time.monotonic() - 1
    old.circuit_open_until = time.monotonic() - 1
    assert new.can_retry() is True and old.can_retry() is True
