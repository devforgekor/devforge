#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for GraduatedRecoveryStrategy."""
from __future__ import annotations

from devforge.domain.watchdog.recovery.strategies import GraduatedRecoveryStrategy
from devforge.ports.types import ComponentState, ComponentStatus


class TestGraduatedRecoveryStrategy:
    def _make_status(self, fail_count: int, state: ComponentState = ComponentState.DEGRADED) -> ComponentStatus:
        return ComponentStatus(
            name="c",
            state=state,
            fail_count=fail_count,
            consecutive_failures=fail_count,
            circuit_open=False,
            last_success=None,
            last_failure=None,
        )

    def test_can_handle_degraded(self) -> None:
        strat = GraduatedRecoveryStrategy()
        assert strat.can_handle("c", self._make_status(1)) is True

    def test_can_handle_critical(self) -> None:
        strat = GraduatedRecoveryStrategy()
        assert strat.can_handle("c", self._make_status(1, ComponentState.CRITICAL)) is True

    def test_cannot_handle_healthy(self) -> None:
        strat = GraduatedRecoveryStrategy()
        assert strat.can_handle("c", self._make_status(0, ComponentState.HEALTHY)) is False

    def test_low_severity_is_restart(self) -> None:
        strat = GraduatedRecoveryStrategy()
        action = strat.recover("c", self._make_status(1))
        assert action.action_type == "restart"
        assert action.severity == 0

    def test_medium_severity_is_reload(self) -> None:
        strat = GraduatedRecoveryStrategy()
        action = strat.recover("c", self._make_status(3))
        assert action.action_type == "reload"
        assert action.severity == 1

    def test_high_severity_is_reset(self) -> None:
        strat = GraduatedRecoveryStrategy()
        action = strat.recover("c", self._make_status(5))
        assert action.action_type == "reset"
        assert action.severity == 2

    def test_severity_increases(self) -> None:
        strat = GraduatedRecoveryStrategy()
        a1 = strat.recover("c", self._make_status(1))
        a5 = strat.recover("c", self._make_status(5))
        assert a5.severity > a1.severity
