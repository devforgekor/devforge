#!/usr/bin/env python3.11
# Status: production
# Path: MCP stdio (mcp.json: exa-search) — thin wrapper over lib/research/exa.py
"""exa_mcp (thin wrapper). Core logic lives in lib.research.exa."""

import json
import os
import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.research import exa

TOOLS = [
    {
        "name": "exa_search",
        "description": "Search the web using Exa. Returns relevant results with titles, URLs, and optional contents (text, highlights, summaries). Supports various search types: auto (default/balanced), fast (low latency), instant (lowest latency), deep (multi-step research), deep-reasoning (complex analysis). Can filter by domain, date range, and category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "type": {"type": "string", "enum": ["auto", "fast", "instant", "deep", "deep-lite", "deep-reasoning"], "description": "Search mode.", "default": "auto"},
                "numResults": {"type": "integer", "description": "Number of results (1-100, default 10)", "minimum": 1, "maximum": 100, "default": 10},
                "includeDomains": {"type": "array", "items": {"type": "string"}, "description": "Only return results from these domains"},
                "excludeDomains": {"type": "array", "items": {"type": "string"}, "description": "Exclude results from these domains"},
                "category": {"type": "string", "enum": ["company", "research paper", "news", "personal site", "financial report", "people"], "description": "Focus search on a specific category"},
                "startPublishedDate": {"type": "string", "description": "Only return results published after this date (ISO 8601)"},
                "endPublishedDate": {"type": "string", "description": "Only return results published before this date (ISO 8601)"},
                "text": {"type": "boolean", "description": "Include full page text in results", "default": False},
                "highlights": {"type": "boolean", "description": "Include relevant highlights/snippets in results", "default": True},
                "summary": {"type": "boolean", "description": "Include an LLM-generated summary of each page", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "exa_get_contents",
        "description": "Retrieve the full text contents of specific URLs using Exa. Returns page text, highlights, and metadata for up to 10 URLs at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {"type": "array", "items": {"type": "string"}, "description": "URLs to retrieve contents for (max 10)"},
                "text": {"type": "boolean", "description": "Include full page text", "default": True},
                "highlights": {"type": "boolean", "description": "Include relevant highlights", "default": True},
            },
            "required": ["urls"],
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


def _not_impl(msg, req_id):
    _send_message({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": f"Method not found: {msg.get('method', '?')}"}})


def main():
    if not exa.available():
        print("[exa_mcp] EXA_API_KEYS not set", file=sys.stderr)
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
                "serverInfo": {"name": "exa-mcp", "version": "1.0.0"}}})
        elif method == "tools/list":
            _send_message({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name", "")
            a = params.get("arguments", {})
            if name == "exa_search":
                result = exa.exa_search_text(
                    a.get("query", ""), type=a.get("type", "auto"),
                    num_results=a.get("numResults", exa.DEFAULT_NUM),
                    include_domains=a.get("includeDomains"), exclude_domains=a.get("excludeDomains"),
                    category=a.get("category"), start_published_date=a.get("startPublishedDate"),
                    end_published_date=a.get("endPublishedDate"), text=bool(a.get("text")),
                    highlights=a.get("highlights", True), summary=bool(a.get("summary")))
            elif name == "exa_get_contents":
                result = exa.exa_contents_text(a.get("urls", []), text=a.get("text", True),
                                               highlights=a.get("highlights", True))
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
            _not_impl(msg, req_id)


if __name__ == "__main__":
    main()
