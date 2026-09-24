#!/usr/bin/env python3
# Status: production
# Path: tests/fitness/test_mcp_inventory.py — specs/mcp-inventory.yaml
"""MCP server inventory validity + live drift (OWASP MCP09 shadow MCP)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_inventory_check import INVENTORY, drift, load_inventory, load_live  # noqa: E402


def _inventory():
    return load_inventory(str(INVENTORY))


def test_inventory_should_have_required_fields():
    servers, _ = _inventory()
    for name, entry in servers.items():
        for field in ("name", "type", "enabled", "approved", "owner", "purpose"):
            assert field in entry, f"{name} missing {field}"


def test_inventory_should_not_enable_unapproved():
    servers, _ = _inventory()
    bad = sorted(n for n, s in servers.items() if s["enabled"] and not s["approved"])
    assert not bad, f"enabled but not approved: {bad}"


def test_live_config_should_match_inventory():
    servers, paths = _inventory()
    live = load_live(paths)
    if not live:
        pytest.skip("no live opencode config on this host")
    result = drift(servers, live)
    assert not (result["unlisted"] or result["enabled_not_approved"]), result
