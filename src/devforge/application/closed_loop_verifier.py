#!/usr/bin/env python3.12
# Status: experimental
# Path: application/watchdog_service.py (composition root)
"""Closed-loop verifier — confirms remediation actually worked.

After a remediation plan executes, the verifier re-checks the component
state. If the component is still unhealthy, the verifier triggers a
rollback or escalation (fail-safe).
"""

from __future__ import annotations

from typing import Any, Optional

from devforge.core.logging import get_logger
from devforge.ports.types import RemediationResult

_log = get_logger(__name__)


class ClosedLoopVerifier:
    """Verifies remediation results and triggers rollback/escalation on failure."""

    def __init__(
        self,
        health_port: Any,
        rollback_port: Optional[Any] = None,
        max_retries: int = 2,
    ) -> None:
        self._health = health_port
        self._rollback = rollback_port
        self._max_retries = max_retries

    async def verify(
        self,
        component: str,
        result: RemediationResult,
        inc_id: Optional[int] = None,
    ) -> RemediationResult:
        """Verify a remediation result; rollback or escalate on failure."""
        if result.success:
            healthy = await self._health.check_health(component)
            if healthy.is_healthy:
                return result
            _log.warning("closed_loop_verify_failed", component=component)
            if self._rollback is not None:
                try:
                    await self._rollback.rollback(component)
                    return RemediationResult(
                        success=False,
                        steps=result.steps,
                        error="verification failed, rolled back",
                        rolled_back=True,
                    )
                except Exception as e:  # noqa: BLE001 — rollback failure is logged
                    _log.warning("closed_loop_rollback_failed", component=component, error=str(e))
            return RemediationResult(
                success=False,
                steps=result.steps,
                error="verification failed",
                rolled_back=False,
            )
        return result
