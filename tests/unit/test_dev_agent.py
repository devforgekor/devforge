#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_dev_agent.py — scripts/dev_agent.py (AGENT_VERSION)
"""AGENT_VERSION constant is exposed and surfaced in the CLI description."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import dev_agent  # noqa: E402


def test_agent_version_is_module_constant():
    assert dev_agent.AGENT_VERSION == "0.1"


def test_agent_version_appears_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        dev_agent.main(["--help"])

    assert exc.value.code == 0
    assert f"v{dev_agent.AGENT_VERSION}" in capsys.readouterr().out
