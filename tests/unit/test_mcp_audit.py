#!/usr/bin/env python3
# Status: production
# Path: tests/unit/test_mcp_audit.py — MCP tool-call audit (OWASP MCP08)
"""MCP tool-call audit helpers: stable args hashing + never-raise behaviour."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from devforge.adapters.driving.mcp import server  # noqa: E402


def test_args_hash_should_be_order_independent():
    assert server._args_hash({"a": 1, "b": 2}) == server._args_hash({"b": 2, "a": 1})


def test_args_hash_should_differ_for_different_args():
    assert server._args_hash({"a": 1}) != server._args_hash({"a": 2})


async def test_audit_should_never_raise(monkeypatch):
    def boom():
        raise RuntimeError("no db in this test")

    monkeypatch.setattr(server, "get_config", boom)
    await server._audit_tool_call("knowledge_search", {"q": "x"}, ok=True)
    await server._audit_tool_call("knowledge_search", {"q": "x"}, ok=False, error="e")
