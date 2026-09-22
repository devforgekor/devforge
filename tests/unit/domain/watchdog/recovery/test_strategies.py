#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/domain/watchdog/recovery/
"""Tests for recovery kind classification (B1)."""
from __future__ import annotations

from devforge.domain.watchdog.recovery.strategies import (
    DefaultRecoveryStrategy,
    classify_recovery_kind,
)


def test_service_kind() -> None:
    assert classify_recovery_kind("svc:devforge-turn-watcher") == "service"


def test_container_kind() -> None:
    assert classify_recovery_kind("svc:container-devforge-fastapi") == "container"


def test_exact_overrides() -> None:
    assert classify_recovery_kind("svc:svc-pod-forwarding") == "svcpod"
    assert classify_recovery_kind("svc:ebook-watcher") == "ebook"
    assert classify_recovery_kind("system:memory") == "oom"


def test_prefix_kinds() -> None:
    assert classify_recovery_kind("timer:devforge-day-cycle.timer") == "timer_kick"
    assert classify_recovery_kind("llm:day-extract") == "cascade"
    assert classify_recovery_kind("oneshot:news-collector") == "oneshot"


def test_alert_only_returns_none() -> None:
    assert classify_recovery_kind("syssvc:caddy") is None
    assert classify_recovery_kind("system:disk:/") is None


def test_alert_only_targets_return_none() -> None:
    """legacy ALERT_ONLY_TARGETS must never be recovered (container/proxy infra)."""
    for comp in ("svc:container-postgres", "svc:container-devforge-mcp",
                 "svc:container-flaresolverr", "svc:anthropic-openrouter-proxy",
                 "svc:anthropic-proxy", "svc:or-rate-limiter"):
        assert classify_recovery_kind(comp) is None, comp


def test_create_action_includes_backoff() -> None:
    a = DefaultRecoveryStrategy().create_action("svc:x", "UNHEALTHY", "inactive", 40)
    assert a is not None and a.kind == "service" and a.backoff_sec == 40
