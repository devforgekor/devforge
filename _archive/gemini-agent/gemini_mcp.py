#!/usr/bin/env python3.11
# Status: experimental
# Path: MCP client (mcp.json / .gemini/settings.json), standalone `gemini_mcp` command
"""gemini_mcp — MCP server exposing DevForge tools via Gemini API.

Converts 11 Gemini function declarations to MCP tools.
Uses stdio transport (JSON-RPC with Content-Length framing).
Start: python3.11 gemini_mcp.py
Register in mcp.json:
  "gemini-mcp": {
    "type": "stdio",
    "command": "python3.11",
    "args": ["/opt/projects/server/scripts/gemini_mcp.py"]
  }
"""

import json, sys

from gemini_core import TOOLS, call_gemini, execute_tool, load_keys, pick_key

SELF = object()  # sentinel


def _gemini_tools_to_mcp():
    """Convert Gemini functionDeclarations to MCP tool format."""
    tools = []
    for wrapper in TOOLS:
        for fd in wrapper.get("functionDeclarations", []):
            params = fd.get("parameters", {"type": "object", "properties": {}, "required": []})
            tools.append({
                "name": fd["name"],
                "description": fd.get("description", ""),
                "inputSchema": params,
            })
    return tools


_MCP_TOOLS = _gemini_tools_to_mcp()


def _read_message():
    """Read one JSON-RPC message from stdin (Content-Length framing)."""
    headers = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()
    length = int(headers.get("content-length", 0))
    if length == 0:
        return None
    body = sys.stdin.read(length)
    return json.loads(body)


def _send_message(msg):
    """Write one JSON-RPC message to stdout with Content-Length framing."""
    body = json.dumps(msg)
    payload = f"Content-Length: {len(body)}\r\n\r\n{body}"
    sys.stdout.write(payload)
    sys.stdout.flush()


def _not_impl(msg, req_id):
    _send_message({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {msg.get('method', '?')}"},
    })


def main():
    keys = load_keys()
    if not keys:
        print("[gemini_mcp] No API keys found", file=sys.stderr)
        sys.exit(1)

    pick_key(keys)

    while True:
        msg = _read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")

        # Notification (no id)
        if req_id is None:
            if method == "notifications/initialized":
                continue
            if method == "notifications/cancelled":
                continue
            continue

        params = msg.get("params", {})

        if method == "initialize":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "gemini-mcp", "version": "1.0.0"},
                },
            })

        elif method == "tools/list":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": _MCP_TOOLS},
            })

        elif method == "tools/call":
            name = (params.get("arguments") or params).get("name", params.get("name", ""))
            arguments = params.get("arguments", params)
            if not name:
                _send_message({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": "Missing tool name"},
                })
                continue
            # Remove 'name' from arguments if present
            if "name" in arguments:
                arguments = {k: v for k, v in arguments.items() if k != "name"}
            result = execute_tool(name, arguments)
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": result}]},
            })

        elif method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": None})
            break

        else:
            _not_impl(msg, req_id)


if __name__ == "__main__":
    main()
