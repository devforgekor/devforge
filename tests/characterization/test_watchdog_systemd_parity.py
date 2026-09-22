#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Characterization test: verify new systemd health adapter matches legacy behavior."""
from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from devforge.adapters.driven.health.systemd_health import (
    SystemdServiceHealthCheck,
    SystemdTimerHealthCheck,
)


class TestSystemdServiceHealthCheckParity:
    """Verify new adapter detects same failures as legacy checker.py."""

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_active_service_detected(self, mock_run: MagicMock) -> None:
        """Active service returns ok=True."""
        mock_run.return_value = MagicMock(returncode=0, stdout="active\n")

        checker = SystemdServiceHealthCheck(["devforge-fastapi"])
        result = await checker.check_health()

        assert result.ok is True
        assert "1 services active" in result.detail

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_inactive_service_detected(self, mock_run: MagicMock) -> None:
        """Inactive service returns ok=False."""
        mock_run.return_value = MagicMock(returncode=3, stdout="inactive\n")

        checker = SystemdServiceHealthCheck(["devforge-fastapi"])
        result = await checker.check_health()

        assert result.ok is False
        assert "devforge-fastapi" in result.detail

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_multiple_services(self, mock_run: MagicMock) -> None:
        """Multiple services - one fails, one passes."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="active\n"),
            MagicMock(returncode=3, stdout="inactive\n"),
        ]

        checker = SystemdServiceHealthCheck(["svc1", "svc2"])
        result = await checker.check_health()

        assert result.ok is False
        assert "svc2" in result.detail


class TestSystemdTimerHealthCheckParity:
    """Verify new timer adapter matches legacy behavior."""

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_active_timer(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="active\n")

        checker = SystemdTimerHealthCheck(["devforge-day-cycle.timer"])
        result = await checker.check_health()

        assert result.ok is True
        assert "1 timers active" in result.detail

    @patch("subprocess.run")
    @pytest.mark.asyncio
    async def test_failed_timer(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=3, stdout="inactive\n")

        checker = SystemdTimerHealthCheck(["devforge-day-cycle.timer"])
        result = await checker.check_health()

        assert result.ok is False
        assert "devforge-day-cycle.timer" in result.detail


# Legacy parity integration test (requires actual systemd)
@pytest.mark.integration
class TestSystemdServiceHealthCheckLegacyParity:
    """Integration test against real systemd (skipped by default)."""

    @pytest.mark.asyncio
    async def test_systemd_service_parity(self) -> None:
        """New adapter should detect same failures as legacy systemctl."""
        # Get actual service status via legacy method
        legacy_result = subprocess.run(
            ["systemctl", "--user", "is-active", "devforge-fastapi"],
            capture_output=True,
        )
        legacy_ok = (legacy_result.returncode == 0)

        # New adapter
        checker = SystemdServiceHealthCheck(["devforge-fastapi"])
        new_result = await checker.check_health()

        assert new_result.ok == legacy_ok