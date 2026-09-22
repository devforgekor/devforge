#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/monitoring/
"""Tests for BackoffCalculator (A2)."""
from __future__ import annotations

from devforge.domain.watchdog.monitoring.backoff import (
    DEFAULT_BACKOFF_SCHEDULE,
    BackoffCalculator,
)


def test_schedule_matches_legacy() -> None:
    assert DEFAULT_BACKOFF_SCHEDULE == [0, 10, 20, 40, 80, 120, 300]


def test_zero_attempt_returns_zero() -> None:
    assert BackoffCalculator().calculate(0) == 0


def test_backoff_jitter_ranges() -> None:
    calc = BackoffCalculator()
    assert 9 <= calc.calculate(1) <= 11      # 10 ±10%
    assert 18 <= calc.calculate(2) <= 22     # 20 ±10%
    assert 36 <= calc.calculate(3) <= 44     # 40 ±10%


def test_backoff_caps_at_max() -> None:
    calc = BackoffCalculator()
    assert 270 <= calc.calculate(100) <= 330  # 300 ±10%


def test_jitter_varies() -> None:
    calc = BackoffCalculator()
    assert len({calc.calculate(3) for _ in range(20)}) > 1


def test_custom_schedule_is_range_asserted() -> None:
    calc = BackoffCalculator(schedule=[5, 10, 15])
    assert 4 <= calc.calculate(0) <= 5
    assert calc.max_backoff() == 15
