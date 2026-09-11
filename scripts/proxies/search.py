#!/usr/bin/env python3
# Status: production
# Path: MCP stdio (mcp.json: search-proxy) — thin wrapper over lib/research/web.py
"""MCP search proxy (thin wrapper). Core logic lives in lib.research.web."""

import json
import os
import sys

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research.web import SearchProxy

TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web with automatic rotation: Brave → Tavily → you.com fallback. "
            "Each provider has independent key rotation. Failed provider → next pick. "
            "For provider-specific search, use brave-search, exa-search, or tavily-search separately."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {
                    "type": "number",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5, "minimum": 1, "maximum": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_stats",
        "description": "Show search proxy key rotation statistics per provider.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
]


def _send(response: dict):
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _log(msg: str):
    print(f"[search_proxy] {msg}", file=sys.stderr, flush=True)


def main():
    proxy = SearchProxy()
    _log("MCP server ready")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = request.get("id")
        method = request.get("method", "")

        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "search-proxy", "version": "1.0.0"}}})
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            if tool_name == "web_search":
                results = proxy.search(arguments.get("query", ""), arguments.get("max_results", 5))
                _send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": r} for r in results]}})
            elif tool_name == "search_stats":
                _send({"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": proxy.stats()}]}})
            else:
                _send({"jsonrpc": "2.0", "id": req_id,
                       "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}})
        elif method == "notifications/initialized":
            pass
        else:
            _send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": f"Unknown method: {method}"}})


if __name__ == "__main__":
    main()
