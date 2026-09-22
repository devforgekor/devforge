#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Characterization test: verify new recovery adapter matches legacy behavior."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
from devforge.domain.watchdog.recovery.strategies import RecoveryAction


class TestSystemdRecoveryAdapterParity:
    """Verify new recovery adapter matches legacy recovery.py behavior."""

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_restart_success(self, mock_run: MagicMock) -> None:
        """Restart action returns True on success."""
        mock_run.return_value = MagicMock(returncode=0)

        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="restart",
            detail="Restart service",
            severity=0,
        )

        result = await adapter.execute_recovery(action)

        assert result is True
        mock_run.assert_called_once()
        # Verify systemctl --user restart was called
        call_args = mock_run.call_args[0][0]
        assert "systemctl" in call_args
        assert "--user" in call_args
        assert "restart" in call_args

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_restart_failure(self, mock_run: MagicMock) -> None:
        """Restart action returns False on failure."""
        mock_run.return_value = MagicMock(returncode=1)

        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="restart",
            detail="Restart service",
            severity=0,
        )

        result = await adapter.execute_recovery(action)

        assert result is False

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_reload_action(self, mock_run: MagicMock) -> None:
        """Reload action uses reload-or-restart."""
        mock_run.return_value = MagicMock(returncode=0)

        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="reload",
            detail="Reload service",
            severity=1,
        )

        result = await adapter.execute_recovery(action)

        assert result is True
        call_args = mock_run.call_args[0][0]
        assert "reload-or-restart" in call_args

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_hard_reset_action(self, mock_run: MagicMock) -> None:
        """Hard reset calls stop, daemon-reload, start."""
        mock_run.return_value = MagicMock(returncode=0)

        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="reset",
            detail="Hard reset",
            severity=2,
        )

        result = await adapter.execute_recovery(action)

        assert result is True
        # Should have called stop, daemon-reload, start (3 calls)
        assert mock_run.call_count == 3

    @pytest.mark.asyncio
    async def test_unknown_action_type(self) -> None:
        """Unknown action type returns False."""
        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="unknown",
            detail="Unknown",
            severity=0,
        )

        result = await adapter.execute_recovery(action)

        assert result is False