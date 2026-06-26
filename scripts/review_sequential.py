#!/usr/bin/env python3
# Status: experimental
# Path: bash — sequential code review via devforge-mcp, feeds Aider prompt crafting
"""Call review_sequential MCP tool and print structured analysis as JSON to stdout."""
import json
import sys
import urllib.request
from typing import Optional, List, Tuple

MCP_URL = "http://127.0.0.1:8000/mcp"


def _post_mcp(method: str, params: Optional[dict] = None,
               session_id: Optional[str] = None) -> Tuple[dict, dict]:
    """POST to MCP endpoint. Returns (parsed_json, response_headers)."""
    body = json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": params or {}}).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    req = urllib.request.Request(MCP_URL, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode()
        resp_headers = dict(resp.headers)
        # Parse SSE: "event: message\ndata: {...}"
        for line in raw.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:]), resp_headers
        return json.loads(raw), resp_headers


def _init_session() -> str:
    result, headers = _post_mcp("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "review-cli", "version": "1.0"},
    })
    # Session ID is in the mcp-session-id response header
    return headers.get("mcp-session-id", "")


def review_sequential(task: str, paths: List[str]) -> dict:
    session_id = _init_session()
    if not session_id:
        return {"error": "failed to initialize MCP session"}
    result, _ = _post_mcp(
        "tools/call",
        {"name": "review_sequential", "arguments": {"task": task, "paths": ",".join(paths)}},
        session_id=session_id,
    )
    content = result.get("result", {}).get("content", [])
    for c in content:
        if c.get("type") == "text":
            return json.loads(c["text"])
    return {"error": "no text content in response", "raw": result}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Sequential code review via devforge-mcp")
    parser.add_argument("task", help="Description of the coding task")
    parser.add_argument("paths", nargs="+", help="File paths to review (comma or space separated)")
    args = parser.parse_args()

    flat_paths = []
    for p in args.paths:
        flat_paths.extend([x.strip() for x in p.split(",") if x.strip()])

    result = review_sequential(args.task, flat_paths)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
