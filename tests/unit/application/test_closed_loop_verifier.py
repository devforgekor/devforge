#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/application/
"""Tests for ClosedLoopVerifier — post-execution verification."""

from __future__ import annotations

from typing import Any

import pytest

from devforge.application.closed_loop_verifier import ClosedLoopVerifier
from devforge.ports.types import RemediationResult, StepResult


class FakeHealth:
    def __init__(self, healthy: bool = True) -> None:
        self._healthy = healthy

    async def check_health(self, component: str) -> Any:
        from devforge.ports.types import HealthCheck

        return HealthCheck(component=component, is_healthy=self._healthy, detail="")


class FakeRollback:
    def __init__(self) -> None:
        self.rolled_back = False

    async def rollback(self, component: str) -> None:
        self.rolled_back = True


def _success_result() -> RemediationResult:
    return RemediationResult(success=True, steps=[StepResult("execute", True)])


def _failure_result() -> RemediationResult:
    return RemediationResult(success=False, steps=[StepResult("execute", False, "timeout")])


@pytest.mark.asyncio
async def test_should_pass_when_verification_succeeds() -> None:
    verifier = ClosedLoopVerifier(FakeHealth(healthy=True))
    result = await verifier.verify("svc:test", _success_result())
    assert result.success


@pytest.mark.asyncio
async def test_should_fail_when_verification_fails() -> None:
    verifier = ClosedLoopVerifier(FakeHealth(healthy=False))
    result = await verifier.verify("svc:test", _success_result())
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_should_rollback_when_verification_fails() -> None:
    rollback = FakeRollback()
    verifier = ClosedLoopVerifier(FakeHealth(healthy=False), rollback_port=rollback)
    result = await verifier.verify("svc:test", _success_result())
    assert not result.success
    assert result.rolled_back
    assert rollback.rolled_back


@pytest.mark.asyncio
async def test_should_not_rollback_when_no_rollback_port() -> None:
    verifier = ClosedLoopVerifier(FakeHealth(healthy=False), rollback_port=None)
    result = await verifier.verify("svc:test", _success_result())
    assert not result.success
    assert not result.rolled_back


@pytest.mark.asyncio
async def test_should_passthrough_failed_result() -> None:
    verifier = ClosedLoopVerifier(FakeHealth(healthy=True))
    result = await verifier.verify("svc:test", _failure_result())
    assert not result.success
