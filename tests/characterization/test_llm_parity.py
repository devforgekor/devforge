#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Parity: LLM health adapter probe vs a live serving port (skips offline)."""
from __future__ import annotations

import socket

import pytest

from devforge.adapters.driven.health.llm_health import LLMHealthChecker

pytestmark = pytest.mark.characterization

_PORT = 8082


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.asyncio
async def test_probe_against_live_port() -> None:
    if not _port_open(_PORT):
        pytest.skip(f"no model serving on :{_PORT}")
    checks = await LLMHealthChecker({"day-extract": _PORT}, timeout=10).check_health()
    assert len(checks) == 1 and checks[0].component == "llm:day-extract"
