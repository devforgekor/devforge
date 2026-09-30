#!/usr/bin/env python3.12
# Status: experimental
# Path: application/watchdog_service.py (composition root)
"""Remediation runner — executes a RemediationPlan step by step (runbook pattern).

Each step runs in order: pre-check → execute → verify → record.
If any step fails, the plan stops and returns a RemediationResult with
rolled_back=True if a rollback action is available.

Closed-loop: after execution, the verifier re-checks the component state
to confirm the remediation actually worked.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from devforge.core.logging import get_logger
from devforge.ports.types import RemediationPlan, RemediationResult, StepResult

_log = get_logger(__name__)


class RemediationRunner:
    """Executes a RemediationPlan with closed-loop verification."""

    def __init__(
        self,
        recovery_port: Any,
        health_port: Any,
        incident_repo: Any,
        rollback_port: Optional[Any] = None,
    ) -> None:
        self._recovery = recovery_port
        self._health = health_port
        self._incidents = incident_repo
        self._rollback = rollback_port

    async def execute(self, plan: RemediationPlan, inc_id: Optional[int] = None) -> RemediationResult:
        """Execute a remediation plan step by step."""
        steps: list[StepResult] = []
        for step in plan.steps:
            result = await self._run_step(step, plan.component)
            steps.append(result)
            if not result.success:
                rolled_back = await self._rollback_steps(steps, plan.component)
                return RemediationResult(
                    success=False,
                    steps=steps,
                    error=f"step '{step.name}' failed: {result.detail}",
                    rolled_back=rolled_back,
                )
        return RemediationResult(success=True, steps=steps)

    async def _run_step(self, step: Any, component: str) -> StepResult:
        """Run a single remediation step."""
        start = time.monotonic()
        try:
            if step.action == "pre-check":
                ok = await self._pre_check(component, step.params)
            elif step.action == "execute":
                ok = await self._execute(component, step.params)
            elif step.action == "verify":
                ok = await self._verify(component, step.params)
            elif step.action == "record":
                ok = await self._record(component, step.params)
            else:
                ok = False
            duration = (time.monotonic() - start) * 1000
            return StepResult(step.name, ok, "", duration)
        except Exception as e:  # noqa: BLE001 — step failure must not crash the plan
            duration = (time.monotonic() - start) * 1000
            _log.warning("remediation_step_failed", step=step.name, error=str(e))
            return StepResult(step.name, False, str(e), duration)

    async def _pre_check(self, component: str, params: dict[str, Any]) -> bool:
        """Re-check component state before acting (level-triggered)."""
        result = await self._health.check_health(component)
        return bool(result.is_healthy)

    async def _execute(self, component: str, params: dict[str, Any]) -> bool:
        """Execute the actual remediation action."""
        return bool(await self._recovery.execute_recovery(component, params))

    async def _verify(self, component: str, params: dict[str, Any]) -> bool:
        """Verify the remediation worked (closed-loop)."""
        result = await self._health.check_health(component)
        return bool(result.is_healthy)

    async def _record(self, component: str, params: dict[str, Any]) -> bool:
        """Record the remediation result."""
        if params.get("inc_id") is not None:
            await self._incidents.record_action(
                params["inc_id"], params.get("action", "remediation"), True
            )
        return True

    async def _rollback_steps(self, completed: list[StepResult], component: str) -> bool:
        """Rollback completed steps if a rollback port is available."""
        if self._rollback is None:
            return False
        try:
            await self._rollback.rollback(component)
            return True
        except Exception as e:  # noqa: BLE001 — rollback failure is logged
            _log.warning("remediation_rollback_failed", component=component, error=str(e))
            return False
