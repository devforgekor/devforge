#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_unified_search_mcp.py
"""Unified search MCP — protocol handshake and tool surface."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "proxies" / "unified_search.py"


def _call(reqs):
    inp = "\n".join(json.dumps(r) for r in reqs) + "\n"
    p = subprocess.run([sys.executable, str(SCRIPT)], input=inp, capture_output=True, text=True, timeout=60)
    return [json.loads(line) for line in p.stdout.splitlines() if line.strip()]


def test_should_expose_three_narrow_tools():
    out = _call([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ])
    tools = [t["name"] for t in out[1]["result"]["tools"]]
    assert tools == ["search", "fetch_contents", "query_docs"]
    assert out[0]["result"]["serverInfo"]["name"] == "search"


def test_should_report_unknown_tool():
    out = _call([{"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                  "params": {"name": "nope", "arguments": {}}}])
    assert out[0]["error"]["code"] == -32601


def test_should_reject_empty_query():
    out = _call([{"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                  "params": {"name": "search", "arguments": {"query": ""}}}])
    assert out[0]["error"]["code"] == -32000


def test_schema_should_stay_compact():
    out = _call([{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
    total = sum(len(json.dumps(t)) for t in out[0]["result"]["tools"])
    assert total < 2000  # 3 tools must stay well under legacy 6-tool footprint
