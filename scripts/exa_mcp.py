#!/usr/bin/env python3.11
"""exa_mcp — MCP server for Exa Search with round-robin key rotation.

Provides:
  - exa_search: Web search via Exa API
  - exa_get_contents: Retrieve page contents by URL

Keys from EXA_API_KEYS env var (label:key,label:key,...).
Register in mcp.json:
  "exa-search": {
    "type": "stdio",
    "command": "python3.11",
    "args": ["/opt/projects/server/scripts/exa_mcp.py"]
  }
"""

import json, os, sys, itertools, time, re
import httpx

# Ensure scripts/ is in path for lib imports when spawned via MCP stdio
_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

KEYS_ENV = "EXA_API_KEYS"
SECRETS_PATH = os.path.expanduser("~/.config/devforge/secrets.env")
DEFAULT_NUM = 10
MAX_NUM = 100
API_BASE = "https://api.exa.ai"


def _ensure_env():
    """Load ENCRYPTION_PASSPHRASE and EXA_API_KEYS from secrets.env if not set."""
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
        print(f"[exa_mcp] {KEYS_ENV} not set", file=sys.stderr)
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
                print(f"[exa_mcp] Failed to decrypt key, trying as plaintext", file=sys.stderr)
                plain = cipher
            keys.append(plain)
    if not keys:
        print(f"[exa_mcp] No keys found in {KEYS_ENV}", file=sys.stderr)
        sys.exit(1)
    return keys


def _read_message():
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


TOOLS = [
    {
        "name": "exa_search",
        "description": "Search the web using Exa. Returns relevant results with titles, URLs, and optional contents (text, highlights, summaries). Supports various search types: auto (default/balanced), fast (low latency), instant (lowest latency), deep (multi-step research), deep-reasoning (complex analysis). Can filter by domain, date range, and category.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query"
                },
                "type": {
                    "type": "string",
                    "enum": ["auto", "fast", "instant", "deep", "deep-lite", "deep-reasoning"],
                    "description": "Search mode. auto (default) balances quality and speed. fast for low latency. instant for real-time. deep for multi-step research. deep-reasoning for complex analysis.",
                    "default": "auto"
                },
                "numResults": {
                    "type": "integer",
                    "description": "Number of results (1-100, default 10)",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 10
                },
                "includeDomains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only return results from these domains (e.g., arxiv.org)"
                },
                "excludeDomains": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exclude results from these domains"
                },
                "category": {
                    "type": "string",
                    "enum": ["company", "research paper", "news", "personal site", "financial report", "people"],
                    "description": "Focus search on a specific category"
                },
                "startPublishedDate": {
                    "type": "string",
                    "description": "Only return results published after this date (ISO 8601)"
                },
                "endPublishedDate": {
                    "type": "string",
                    "description": "Only return results published before this date (ISO 8601)"
                },
                "text": {
                    "type": "boolean",
                    "description": "Include full page text in results",
                    "default": False
                },
                "highlights": {
                    "type": "boolean",
                    "description": "Include relevant highlights/snippets in results",
                    "default": True
                },
                "summary": {
                    "type": "boolean",
                    "description": "Include an LLM-generated summary of each page",
                    "default": False
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "exa_get_contents",
        "description": "Retrieve the full text contents of specific URLs using Exa. Returns page text, highlights, and metadata for up to 10 URLs at once.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs to retrieve contents for (max 10)"
                },
                "text": {
                    "type": "boolean",
                    "description": "Include full page text",
                    "default": True
                },
                "highlights": {
                    "type": "boolean",
                    "description": "Include relevant highlights",
                    "default": True
                }
            },
            "required": ["urls"]
        }
    }
]


def _call_exa_api(key, endpoint, payload):
    """Call Exa API with given key, return parsed JSON response."""
    headers = {
        "x-api-key": key,
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{API_BASE}{endpoint}", headers=headers, json=payload)
        if resp.status_code == 429:
            return {"_rate_limited": True, "status": 429}
        resp.raise_for_status()
        return resp.json()


def _handle_exa_search(args, keys):
    query = args.get("query", "").strip()
    if not query:
        return "Error: query is required"

    payload = {
        "query": query,
        "type": args.get("type", "auto"),
        "numResults": min(args.get("numResults", DEFAULT_NUM), MAX_NUM),
    }
    if args.get("includeDomains"):
        payload["includeDomains"] = args["includeDomains"]
    if args.get("excludeDomains"):
        payload["excludeDomains"] = args["excludeDomains"]
    if args.get("category"):
        payload["category"] = args["category"]
    if args.get("startPublishedDate"):
        payload["startPublishedDate"] = args["startPublishedDate"]
    if args.get("endPublishedDate"):
        payload["endPublishedDate"] = args["endPublishedDate"]

    contents = {}
    if args.get("text"):
        contents["text"] = True
    if args.get("highlights", True):
        contents["highlights"] = True
    if args.get("summary"):
        contents["summary"] = {}
    if contents:
        payload["contents"] = contents

    # Try keys in round-robin, rotate on 429
    start_idx = keys["idx"]
    for i in range(len(keys["pool"])):
        idx = (start_idx + i) % len(keys["pool"])
        key = keys["pool"][idx]
        result = _call_exa_api(key, "/search", payload)
        if isinstance(result, dict) and result.get("_rate_limited"):
            continue
        keys["idx"] = (idx + 1) % len(keys["pool"])
        # Format results
        results = result.get("results", [])
        lines = []
        for r in results:
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            published = r.get("publishedDate", "")
            preview = ""
            if r.get("highlights"):
                preview = "\n    Highlights: " + " | ".join(r["highlights"][:2])
            elif r.get("text"):
                preview = "\n    Text: " + r["text"][:200] + "..." if len(r.get("text", "")) > 200 else "\n    Text: " + r.get("text", "")
            elif r.get("summary"):
                preview = "\n    Summary: " + r["summary"][:200]
            lines.append(f"- {title}\n  URL: {url}\n  Published: {published}{preview}")
        meta = f"Search results for: {query} (type={payload['type']}, numResults={payload['numResults']}, key_idx={idx})"
        return meta + "\n\n" + "\n\n".join(lines) if lines else meta + "\n\nNo results found."

    return f"Error: All {len(keys['pool'])} Exa API keys rate-limited. Try again later."


def _handle_exa_get_contents(args, keys):
    urls = args.get("urls", [])
    if not urls:
        return "Error: urls is required"
    urls = urls[:10]

    payload = {
        "urls": urls,
    }
    contents = {}
    if args.get("text", True):
        contents["text"] = {"maxCharacters": 5000}
    if args.get("highlights", True):
        contents["highlights"] = True
    if contents:
        payload["contents"] = contents

    start_idx = keys["idx"]
    for i in range(len(keys["pool"])):
        idx = (start_idx + i) % len(keys["pool"])
        key = keys["pool"][idx]
        result = _call_exa_api(key, "/contents", payload)
        if isinstance(result, dict) and result.get("_rate_limited"):
            continue
        keys["idx"] = (idx + 1) % len(keys["pool"])
        results = result.get("results", [])
        lines = []
        for r in results:
            title = r.get("title", "Untitled")
            url = r.get("url", "")
            text = r.get("text", "")
            highlights = r.get("highlights", [])
            lines.append(f"# {title}\nURL: {url}")
            if highlights:
                lines.append("Highlights: " + " | ".join(highlights[:3]))
            if text:
                lines.append("---\n" + text[:3000] + ("..." if len(text) > 3000 else ""))
        return "\n\n".join(lines) if lines else "No contents retrieved."

    return f"Error: All {len(keys['pool'])} Exa API keys rate-limited."


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
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "exa-mcp", "version": "1.0.0"},
                },
            })

        elif method == "tools/list":
            _send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            })

        elif method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            if not name:
                raw = params
                name = raw.get("name", "")
                arguments = raw

            if name == "exa_search":
                result = _handle_exa_search(arguments, keys)
            elif name == "exa_get_contents":
                result = _handle_exa_get_contents(arguments, keys)
            else:
                _send_message({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": f"Unknown tool: {name}"},
                })
                continue

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
