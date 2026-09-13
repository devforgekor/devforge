#!/usr/bin/env python3
# Status: experimental
# Path: tests/conftest.py — shared pytest fixtures for all test_*.py files
"""Shared pytest fixtures for DevForge server tests."""

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"


# ── automatic path setup ──────────────────────────────────────────────
# Any test file under tests/ can `from lib.db import psql` etc.
_scripts_added = False


def pytest_configure(config: Any) -> None:
    """Register custom markers and add scripts/ to sys.path once."""
    global _scripts_added
    if not _scripts_added:
        scripts_str = str(SCRIPTS_DIR)
        if scripts_str not in sys.path:
            sys.path.insert(0, scripts_str)
        _scripts_added = True

    config.addinivalue_line("markers", "integration: marks tests that hit real DB / LLM / services")
    config.addinivalue_line("markers", "slow: marks slow tests (> 10s) — use -m 'not slow' to skip")


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the server project root."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def scripts_dir() -> Path:
    """Absolute path to the scripts/ directory."""
    return SCRIPTS_DIR


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """A writable temp directory for file-generation tests."""
    return tmp_path


# ── CLI runner ────────────────────────────────────────────────────────


@pytest.fixture
def cli_runner(scripts_dir: Path) -> dict:
    """
    Returns helper methods to invoke cli.py commands and capture output.

    Usage::

        def test_something(cli_runner):
            result = cli_runner["run"](["status", "--json"])
            assert result["returncode"] == 0
            import json; data = json.loads(result["stdout"])
    """

    def _run(args: list[str], input_text: str | None = None) -> dict:
        cmd = [sys.executable, str(scripts_dir / "cli.py"), *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                input=input_text,
                timeout=30,
            )
        except subprocess.TimeoutExpired as e:
            return {"returncode": -1, "stdout": e.stdout or "", "stderr": "TIMEOUT"}

        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }

    return {"run": _run}


# ── DB query helper (integration only) ────────────────────────────────


@pytest.fixture
def db_query() -> dict:
    """
    Returns a helper to run SQL against the live PostgreSQL container.

    Mark test with ``@pytest.mark.integration`` so it is skipped by default.

    Usage::

        @pytest.mark.integration
        def test_db_connection(db_query):
            result = db_query["run"]("SELECT 1 AS ok")
            assert result["returncode"] == 0
    """

    def _run(sql: str, db: str = "devforge_app") -> dict:
        cmd = [
            "podman", "exec", "postgres",
            "psql", "-U", "devforge", "-d", db,
            "-c", sql,
            "--tuples-only",
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            return {"returncode": -1, "stdout": "", "stderr": "TIMEOUT"}

        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }

    _run.skip_reason = "integration test — requires running postgres container"
    return {"run": _run}
