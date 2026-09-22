#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for svc-pod port forwarding checker (gap #1)."""
from __future__ import annotations

import pytest

from devforge.adapters.driven.health import svcpod_health
from devforge.adapters.driven.health.svcpod_health import SvcpodForwardingHealthChecker


@pytest.mark.asyncio
async def test_all_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svcpod_health, "_tcp_connect", lambda p, t: True)
    checks = await SvcpodForwardingHealthChecker({8000: "mcp", 8002: "api"}).check_health()
    assert checks[0].is_healthy and checks[0].component == "svc:svc-pod-forwarding"
    assert "2 ports forwarded" in checks[0].detail


@pytest.mark.asyncio
async def test_one_port_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svcpod_health, "_tcp_connect", lambda p, t: p != 8000)
    checks = await SvcpodForwardingHealthChecker({8000: "mcp", 8002: "api"}).check_health()
    assert checks[0].is_healthy is False and "8000(mcp)" in checks[0].detail
