#!/usr/bin/env python3.11
"""context7_mcp — MCP server for Context7 documentation with round-robin key rotation.

Provides:
  - resolve_library_id: Resolve a library name to Context7 library ID
  - query_docs: Query documentation for a specific library

Keys from CONTEXT7_API_KEYS in secrets.env (AES-256-GCM encrypted).
Register in mcp.json:
  "context7": {
    "type": "stdio",
    "command": "python3.11",
    "args": ["/opt/projects/server/scripts/context7_mcp.py"]
  }
"""

import json
import os
import sys

import httpx

# Ensure scripts/ is in path for lib imports when spawned via MCP stdio
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

SECRETS_PATH = os.path.expanduser("~/.config/devforge/secrets.env")
API_BASE = "https://context7.com/api"
KEYS_ENV = "CONTEXT7_API_KEYS"


def _ensure_env():
    if os.environ.get("ENCRYPTION_PASSPHRASE") and os.environ.get(KEYS_ENV):
        return
    if not os.path.exists(SECRETS_PATH):
        return
    with open(SECRETS_PATH) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("ENCRYPTION_PASSPHRASE="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ.setdefault("ENCRYPTION_PASSPHRASE", val)
            elif line.startswith(f"{KEYS_ENV}="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ.setdefault(KEYS_ENV, val)


def _load_keys():
    _ensure_env()
    from lib.auth.api_key_cipher import decrypt_data

    raw = os.environ.get(KEYS_ENV, "")
    if not raw:
        print(f"[context7_mcp] {KEYS_ENV} not set", file=sys.stderr)
        sys.exit(1)
    keys = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            _, cipher = pair.split(":", 1)
            cipher = cipher.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                print("[context7_mcp] Failed to decrypt key, trying as plaintext", file=sys.stderr)
                plain = cipher
            keys.append(plain)
    if not keys:
        print(f"[context7_mcp] No keys found in {KEYS_ENV}", file=sys.stderr)
        sys.exit(1)
    return keys


TOOLS = [
    {
        "name": "resolve_library_id",
        "description": "Resolves a package/product name to a Context7-compatible library ID. Call this before query_docs to get the correct library ID. Returns matching libraries with descriptions, snippet counts, and reputation scores.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The question or task you need help with (used for relevance ranking)",
                },
                "libraryName": {
                    "type": "string",
                    "description": "Library name to search for. Use the official name (e.g., 'Next.js', 'FastAPI', 'React')",
                },
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
                "libraryId": {
                    "type": "string",
                    "description": "Context7 library ID (e.g., '/vercel/next.js', '/mongodb/docs'). Get this from resolve_library_id.",
                },
                "query": {
                    "type": "string",
                    "description": "Your specific question about this library",
                },
            },
            "required": ["libraryId", "query"],
        },
    },
]


def _read_message():
    """Read JSON-RPC message from stdin. Supports both modern JSON-line
    transport (one JSON per line) and legacy Content-Length header format."""
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            continue
        # Modern JSON-line format: line is a complete JSON object
        if stripped.startswith("{"):
            return json.loads(stripped)
        # Legacy Content-Length header format
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
        body = sys.stdin.read(length)
        return json.loads(body)


def _send_message(msg):
    body = json.dumps(msg)
    sys.stdout.write(body + "\n")
    sys.stdout.flush()


def _call_api(path, params, keys, method="GET"):
    start_idx = keys["idx"]
    for i in range(len(keys["pool"])):
        idx = (start_idx + i) % len(keys["pool"])
        key = keys["pool"][idx]
        headers = {"x-api-key": key}

        url = f"{API_BASE}{path}"
        try:
            with httpx.Client(timeout=20.0) as client:
                if method == "GET":
                    resp = client.get(url, params=params, headers=headers)
                else:
                    resp = client.post(url, json=params, headers=headers)
                if resp.status_code == 429:
                    continue
                resp.raise_for_status()
                keys["idx"] = (idx + 1) % len(keys["pool"])
                return resp.json()
        except Exception as e:
            print(f"[context7_mcp] Key {idx} failed: {e}", file=sys.stderr)
            continue
    return None


def _handle_resolve_library_id(args, keys):
    query = args.get("query", "").strip()
    libraryName = args.get("libraryName", "").strip()
    if not libraryName:
        return "Error: libraryName is required"

    params = {"query": query or libraryName, "libraryName": libraryName}
    data = _call_api("/v2/libs/search", params, keys)
    if data is None:
        return "Error: All Context7 API keys exhausted or API unavailable."

    results = data.get("results", [])
    if not results:
        return f"No libraries found matching '{libraryName}'."

    lines = [f"Available libraries for '{libraryName}':\n"]
    for r in results:
        lines.append(f"- {r.get('title', 'Untitled')}")
        lines.append(f"  ID: {r.get('id', 'N/A')}")
        lines.append(f"  Description: {r.get('description', '')[:200]}")
        lines.append(
            f"  Snippets: {r.get('totalSnippets', 0)} | "
            f"Reputation: {r.get('trustScore', 'N/A')} | "
            f"Score: {r.get('benchmarkScore', 'N/A')}"
        )
        versions = r.get("versions", [])
        if versions:
            lines.append(
                f"  Versions: {', '.join(versions[:5])}{'...' if len(versions) > 5 else ''}"
            )
        lines.append("")

    return "\n".join(lines)


def _handle_query_docs(args, keys):
    libraryId = args.get("libraryId", "").strip()
    query = args.get("query", "").strip()
    if not libraryId:
        return "Error: libraryId is required"

    params = {"libraryId": libraryId, "query": query or "general usage"}
    data = _call_api("/v2/context", params, keys)
    if data is None:
        return "Error: All Context7 API keys exhausted or API unavailable."

    text = data.get("data", "")
    if not text:
        return f"No documentation found for library ID '{libraryId}'."

    return text[:10000] + ("..." if len(text) > 10000 else "")


def main():
    pool = _load_keys()
    keys = {"pool": pool, "idx": 0}

    while True:
        msg = _read_message()
        if msg is None:
            break

        method = msg.get("method", "")
        req_id = msg.get("id")

        if req_id is None:
            if method in ("notifications/initialized", "notifications/cancelled"):
                continue
            continue

        params = msg.get("params", {})

        if method == "initialize":
            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "context7-mcp", "version": "1.0.0"},
                    },
                }
            )

        elif method == "tools/list":
            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": TOOLS},
                }
            )

        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            if not name:
                raw = params
                name = raw.get("name", "")
                arguments = raw

            if name == "resolve_library_id":
                result = _handle_resolve_library_id(arguments, keys)
            elif name == "query_docs":
                result = _handle_query_docs(arguments, keys)
            else:
                _send_message(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                    }
                )
                continue

            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": result}]},
                }
            )

        elif method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": None})
            break

        else:
            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    main()
