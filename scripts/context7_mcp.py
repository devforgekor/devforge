#!/usr/bin/env python3.11
# Status: production
# Path: MCP stdio (mcp.json: context7) — thin wrapper over lib/research/context7.py
"""context7_mcp (thin wrapper). Core logic lives in lib.research.context7."""

import json
import os
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research import context7

TOOLS = [
    {
        "name": "resolve_library_id",
        "description": "Resolves a package/product name to a Context7-compatible library ID. Call this before query_docs to get the correct library ID. Returns matching libraries with descriptions, snippet counts, and reputation scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The question or task you need help with (used for relevance ranking)"},
                "libraryName": {"type": "string", "description": "Library name to search for. Use the official name (e.g., 'Next.js', 'FastAPI', 'React')"},
            },
            "required": ["query", "libraryName"],
        },
    },
    {
        "name": "query_docs",
        "description": "Query up-to-date documentation and code examples for a specific library using its Context7 library ID. Use resolve_library_id first to get the correct library ID unless the user provides one directly (format: /org/project).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "libraryId": {"type": "string", "description": "Context7 library ID (e.g., '/vercel/next.js', '/mongodb/docs')."},
                "query": {"type": "string", "description": "Your specific question about this library"},
            },
            "required": ["libraryId", "query"],
        },
    },
]


def _read_message():
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("{"):
            return json.loads(stripped)
        headers = {}
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        while True:
            line = sys.stdin.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", 0))
        if length == 0:
            return None
        return json.loads(sys.stdin.read(length))


def _send_message(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    if not context7.available():
        print("[context7_mcp] CONTEXT7_API_KEYS not set", file=sys.stderr)
        sys.exit(1)

    while True:
        msg = _read_message()
        if msg is None:
            break
        method = msg.get("method", "")
        req_id = msg.get("id")
        if req_id is None:
            continue
        params = msg.get("params", {})

        if method == "initialize":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "context7-mcp", "version": "1.0.0"}}})
        elif method == "tools/list":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name", "")
            a = params.get("arguments", {})
            if name == "resolve_library_id":
                result = context7.resolve_library_id_text(a.get("query", ""), a.get("libraryName", ""))
            elif name == "query_docs":
                result = context7.query_docs_text(a.get("libraryId", ""), a.get("query", ""))
            else:
                _send_message({"jsonrpc": "2.0", "id": req_id,
                               "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
                continue
            _send_message({"jsonrpc": "2.0", "id": req_id,
                           "result": {"content": [{"type": "text", "text": result}]}})
        elif method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": None})
            break
        else:
            _send_message({"jsonrpc": "2.0", "id": req_id,
                           "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()
