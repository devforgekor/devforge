#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/domain/watchdog/
"""Tests for A/B/C routing (S1, pure)."""
from __future__ import annotations

import pytest

from devforge.domain.watchdog.routing import matches_canary, route


@pytest.mark.parametrize(
    ("component", "kind"),
    [
        ("svc:devforge-day-cycle", "service"),
        ("svc:container-devforge-worker", "container"),
        ("svc:ebook-watcher", "ebook"),
        ("llm:day-extract", "cascade"),
        ("infra:inference", "cascade"),
        ("pipeline:embed", "pipeline"),
    ],
)
def test_should_route_mutating_recovery_to_fix(component: str, kind: str) -> None:
    decision = route(component, "down")
    assert decision.logic == "fix"
    assert decision.kind == kind
    assert decision.impact == "mutating"
    assert decision.terminal is False


@pytest.mark.parametrize(
    ("component", "kind"),
    [
        ("oneshot:devforge-backup.service", "oneshot_run"),
        ("timer:devforge-system-sync.timer", "timer_kick"),
    ],
)
def test_should_route_scheduled_units_to_catchup(component: str, kind: str) -> None:
    decision = route(component, "failed")
    assert decision.logic == "catchup"
    assert decision.kind == kind


@pytest.mark.parametrize(
    "component",
    [
        "syssvc:caddy",
        "system:disk:/",
        "dataimpulse:toki31",
        "svc:container-postgres",  # exact alert-only
        "unknown:thing",
    ],
)
def test_should_route_non_actionable_to_alert(component: str) -> None:
    decision = route(component, "down")
    assert decision.logic == "alert"
    assert decision.kind is None
    assert decision.impact == "non-mutating"


def test_should_mark_terminal_on_config_errors() -> None:
    assert route("svc:x", "down", "start request repeated: Permission denied").terminal is True
    assert route("svc:x", "down", "unit not found").terminal is True
    assert route("svc:x", "down", "connection refused").terminal is False


def test_matches_canary_exact_and_prefix() -> None:
    assert matches_canary("svc:svc-pod-forwarding", ["svc:svc-pod-forwarding"]) is True
    assert matches_canary("svc:container-x", ["svc:container-"]) is True
    assert matches_canary("llm:day-extract", ["svc:"]) is False
    assert matches_canary("svc:x", []) is False
