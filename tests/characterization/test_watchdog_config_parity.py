#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: v2.1 WatchdogConfig targets vs legacy lib.watchdog.config.

Guards against the cutover gap where v2.1 monitored different targets than
the production legacy watchdog (e.g. non-existent cycle timers).
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.characterization

legacy = pytest.importorskip("lib.watchdog.config")

from devforge.core.config import WatchdogConfig  # noqa: E402


def test_timers_match_legacy() -> None:
    assert set(WatchdogConfig().timers) == set(legacy.TIMER_TARGETS)


def test_timer_max_idle_matches_legacy() -> None:
    cfg = WatchdogConfig().timers
    for unit, spec in legacy.TIMER_TARGETS.items():
        assert cfg[unit] == spec["max_idle"], unit


def test_critical_services_match_legacy() -> None:
    assert set(WatchdogConfig().critical_services) == set(legacy.SERVICE_TARGETS)


def test_system_service_targets_match_legacy() -> None:
    assert set(WatchdogConfig().system_service_targets) == set(legacy.SYSTEM_SERVICE_TARGETS)


def test_alert_only_targets_match_legacy() -> None:
    assert set(WatchdogConfig().alert_only_targets) == set(legacy.ALERT_ONLY_TARGETS)


def test_oneshot_result_targets_match_legacy() -> None:
    assert set(WatchdogConfig().oneshot_result_targets) == set(legacy.ONESHOT_RESULT_TARGETS)


def test_heartbeat_workers_match_legacy() -> None:
    assert WatchdogConfig().heartbeat_workers == legacy.HEARTBEAT_WORKERS


def test_llm_targets_match_legacy() -> None:
    cfg = WatchdogConfig().llm_targets
    for label, spec in legacy.LLM_TARGETS.items():
        assert cfg[label] == spec["port"], label


def test_day_ports_match_legacy() -> None:
    assert set(WatchdogConfig().day_ports) == set(legacy.DAY_PORTS)


def test_svcpod_ports_match_legacy() -> None:
    assert WatchdogConfig().svcpod_published_ports == legacy.SVCPOD_PUBLISHED_PORTS
