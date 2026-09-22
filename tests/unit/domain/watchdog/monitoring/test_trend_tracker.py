#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/monitoring/
"""Tests for TrendTracker (A4)."""
from __future__ import annotations

from devforge.domain.watchdog.monitoring.tracker import TrendTracker


def test_insufficient_samples_none() -> None:
    t = TrendTracker()
    for i in range(9):
        t.add(float(i), float(i))
    assert t.predict_eta(100.0) is None


def test_flat_trend_none() -> None:
    t = TrendTracker()
    for i in range(15):
        t.add(50.0, float(i))
    assert t.predict_eta(100.0) is None


def test_descending_trend_none() -> None:
    t = TrendTracker()
    for i in range(15):
        t.add(100.0 - i * 5, float(i))
    assert t.predict_eta(90.0) is None


def test_ascending_predicts_minutes() -> None:
    # +1/min from 50; last sample = 64 at t=840s. threshold=80.
    # legacy uses the LATEST value, so ETA = (80-64)/(1/60)/60 = 16 min.
    t = TrendTracker()
    for i in range(15):
        t.add(50.0 + i, float(i * 60))
    eta = t.predict_eta(80.0)
    assert eta is not None and 15 <= eta <= 17


def test_breached_and_rising_returns_zero() -> None:
    t = TrendTracker()
    for i in range(15):
        t.add(100.0 + i, float(i * 60))
    assert t.predict_eta(90.0) == 0.0


def test_breached_but_descending_returns_none() -> None:
    """Parity guard: legacy checks slope first, so a breached-but-falling metric is None."""
    t = TrendTracker()
    for i in range(15):
        t.add(100.0 - i, float(i * 60))
    assert t.predict_eta(90.0) is None


def test_clear() -> None:
    t = TrendTracker()
    for i in range(15):
        t.add(float(i), float(i))
    t.clear()
    assert t.latest() is None
