#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/
"""Tests for remediation governance (S4, pure)."""
from __future__ import annotations

from devforge.domain.watchdog.governance import escalate_needed


def test_should_escalate_at_threshold() -> None:
    assert escalate_needed(5, 5) is True
    assert escalate_needed(6, 5) is True


def test_should_not_escalate_below_threshold() -> None:
    assert escalate_needed(4, 5) is False


def test_should_never_escalate_when_disabled() -> None:
    assert escalate_needed(100, 0) is False
