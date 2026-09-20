# Status: experimental
# Path: none — test script
"""Test action_queue: write, claim, execute, complete/fail cycle."""

import subprocess
from unittest.mock import MagicMock, patch

from lib.action_queue import (
    action_claim_pending,
    action_complete,
    action_fail,
    action_poll_results,
    action_write,
    execute_action,
)

# ── Execution dispatch tests (unit, mocked subprocess) ────────


class TestExecDispatch:
    """Test execute_action dispatches to correct executor with safe params."""

    def _mock_run(self, returncode=0, stdout="ok", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    @patch("lib.action_queue.subprocess.run")
    def test_systemctl_restart(self, mock_run):
        mock_run.return_value = self._mock_run(0, "ok")
        ok, msg = execute_action(
            {
                "action_type": "systemctl",
                "action_params": {"service": "devforge-inference", "command": "restart"},
            }
        )
        assert ok
        assert "OK" in msg
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == ["systemctl", "--user", "restart", "devforge-inference"]

    @patch("lib.action_queue.subprocess.run")
    def test_systemctl_invalid_service(self, mock_run):
        """Shell injection chars in service name must be rejected."""
        ok, msg = execute_action(
            {
                "action_type": "systemctl",
                "action_params": {"service": "foo; rm -rf /", "command": "restart"},
            }
        )
        assert not ok
        assert "Invalid" in msg
        mock_run.assert_not_called()

    @patch("lib.action_queue.subprocess.run")
    def test_podman_restart(self, mock_run):
        mock_run.return_value = self._mock_run(0, "ok")
        ok, msg = execute_action(
            {
                "action_type": "podman",
                "action_params": {"container": "postgres", "command": "restart"},
            }
        )
        assert ok
        assert "OK" in msg
        args = mock_run.call_args[0][0]
        assert args == ["podman", "restart", "postgres"]

    @patch("lib.action_queue.subprocess.run")
    def test_podman_invalid_container(self, mock_run):
        ok, msg = execute_action(
            {
                "action_type": "podman",
                "action_params": {"container": "$(rm -rf)", "command": "restart"},
            }
        )
        assert not ok
        assert "Invalid" in msg
        mock_run.assert_not_called()

    @patch("lib.action_queue.subprocess.run")
    def test_cli_execute(self, mock_run):
        mock_run.return_value = self._mock_run(0, "task updated")
        ok, msg = execute_action(
            {
                "action_type": "cli",
                "action_params": {"script": "cli.py", "args": ["task", "update", "test"]},
            }
        )
        assert ok
        assert "task updated" in msg
        args = mock_run.call_args[0][0]
        assert args == ["python3", "/opt/projects/server/scripts/cli.py", "task", "update", "test"]

    @patch("lib.action_queue.subprocess.run")
    def test_cli_timed_out(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("cli.py", 120)
        ok, msg = execute_action(
            {
                "action_type": "cli",
                "action_params": {"script": "cli.py", "args": ["status"]},
            }
        )
        assert not ok
        assert "timed out" in msg

    def test_unknown_action_type(self):
        ok, msg = execute_action(
            {
                "action_type": "nonexistent",
                "action_params": {},
            }
        )
        assert not ok
        assert "Unknown" in msg


# ── DB lifecycle tests (integration, real DB) ─────────────────


class TestActionDBCycle:
    """Test write → claim → complete/fail using real watchdog_pulses.

    These are integration tests that require a running PostgreSQL.
    """

    def test_action_write_and_complete(self):
        """Full cycle: write action → claim → complete."""
        pid = action_write(
            instruction="Test action: restart pod B",
            priority="P2_LOW",
            action_type="systemctl",
            action_params={"service": "devforge-inference", "command": "restart"},
            max_retries=1,
        )
        assert pid is not None
        assert pid.startswith("action_")

        try:
            # Claim
            actions = action_claim_pending(max_count=10)
            matching = [a for a in actions if a["pulse_id"] == pid]
            assert len(matching) == 1
            assert matching[0]["action_type"] == "systemctl"
            assert matching[0]["action_params"]["service"] == "devforge-inference"

            # Complete
            ok = action_complete(pid, "Test: completed")
            assert ok

            # Poll results
            results = action_poll_results(pid)
            assert any("Test: completed" in r.get("observation", "") for r in results)
        finally:
            from lib.db import psql_ok

            psql_ok(f"DELETE FROM watchdog_pulses WHERE pulse_id = '{pid}'")

    def test_action_fail_and_escalate(self):
        """Max retries exceeded → HUMAN_REQUIRED."""
        pid = action_write(
            instruction="Test action: escalate",
            priority="P2_LOW",
            action_type="systemctl",
            action_params={"service": "devforge-test", "command": "restart"},
            max_retries=2,
        )
        assert pid is not None

        try:
            # Claim it once
            actions = action_claim_pending()
            assert any(a["pulse_id"] == pid for a in actions)

            # Fail twice to exhaust retries
            for i in range(2):
                ok = action_fail(pid, f"Test error #{i + 1}")
                assert ok

            # Verifiy final status is HUMAN_REQUIRED
            from lib.db import psql_json

            rows = psql_json(
                f"SELECT status, retry_count FROM watchdog_pulses WHERE pulse_id = '{pid}'"
            )
            assert rows
            assert rows[0]["status"] == "HUMAN_REQUIRED"
        finally:
            from lib.db import psql_ok

            psql_ok(f"DELETE FROM watchdog_pulses WHERE pulse_id = '{pid}'")
