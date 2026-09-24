#!/usr/bin/env python3
# Status: production
# Path: tests/fitness/test_mcp_contract.py — specs/mcp-contract.json + specs/mcp-tools.snapshot.json
"""MCP tool-definition contract (OWASP MCP03 tool poisoning).

Treats tool descriptions/schemas as untrusted input: any added/changed tool
fails CI until the snapshot is reviewed and updated (human approval). Also
checks the frozen consolidation contract still references live tools.
"""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from devforge.adapters.driving.mcp import server  # noqa: E402

CONTRACT = ROOT / "specs" / "mcp-contract.json"
SNAPSHOT = ROOT / "specs" / "mcp-tools.snapshot.json"


def _definition_hash(tool: dict) -> str:
    payload = json.dumps(
        {"description": tool["description"], "inputSchema": tool["inputSchema"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _current() -> dict:
    return {t["name"]: _definition_hash(t) for t in server.get_tools()}


def test_contract_tools_should_exist_in_server():
    contract = json.loads(CONTRACT.read_text())
    names = {t["name"] for t in contract["tools"]}
    missing = sorted(names - set(_current()))
    assert not missing, f"contract tools no longer in the MCP server: {missing}"


def test_tool_definitions_should_match_snapshot():
    snapshot = json.loads(SNAPSHOT.read_text())
    current = _current()
    added = sorted(set(current) - set(snapshot))
    removed = sorted(set(snapshot) - set(current))
    changed = sorted(n for n in current if n in snapshot and current[n] != snapshot[n])
    assert not (added or removed or changed), (
        f"MCP tool definitions changed — review and update {SNAPSHOT.name}: "
        f"added={added} removed={removed} changed={changed}"
    )
