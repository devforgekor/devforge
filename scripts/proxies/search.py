#!/usr/bin/env python3
# Status: production
# Path: Caddy reverse-proxy
"""
MCP search proxy: Brave + Tavily (Tier 1) with you.com (Tier 2 fallback).

Per-provider key rotation + combined web_search rotation tool.
For Exa, use exa-search MCP separately.
For Brave, use brave-search MCP separately.
For Tavily, use tavily-search MCP separately.
"""

import html
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# Ensure scripts/ is in path for lib imports when spawned via MCP stdio
_SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.auth.key_rotator import KeyRotator
from lib.auth.api_key_cipher import decrypt_data

# Each provider: env prefix, REST endpoint, auth style, max results.
PROVIDERS = {
    "brave": {
        "prefix": "BRAVE",
        "search_url": "https://api.search.brave.com/res/v1/web/search",
        "auth_header": "X-Subscription-Token",
        "max_results": 20,
        "method": "GET",
        "params": lambda q, n: {"q": q, "count": str(n)},
    },
    "tavily": {
        "prefix": "TRAVILY",
        "search_url": "https://api.tavily.com/search",
        "auth_key_field": "api_key",
        "max_results": 10,
        "method": "POST",
        "body": lambda q, n, key: json.dumps(
            {"api_key": key, "query": q, "search_depth": "basic", "max_results": n}
        ),
        "headers": {"Content-Type": "application/json"},
    },
    "youcom": {
        "prefix": "YOUCOM",
        "search_url": "https://api.you.com/search",
        "auth_header": "X-API-Key",
        "max_results": 10,
        "method": "GET",
        "params": lambda q, n: {"q": q, "count": str(n)},
        "tier": 2,
    },
}

STATE_DIR = os.path.expanduser("~/.cache/devforge")

# you.com: Tier 2 fallback — preload with high calls so they sort last
YOUCOM_CALLS_PRELOAD = 1_000_000


def _load_keys_for(service: str) -> list[tuple[str, str]]:
    """Load API keys for a single provider from secrets.env."""
    cfg = PROVIDERS.get(service)
    if not cfg:
        return []
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    if not os.path.exists(secrets_path):
        return []

    env_name = f"{cfg['prefix']}_API_KEYS"
    keys_str = ""
    with open(secrets_path) as f:
        for line in f:
            if line.startswith(f"{env_name}="):
                keys_str = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not keys_str:
        return []

    keys = []
    for item in keys_str.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, cipher = item.split(":", 1)
            plain = decrypt_data(cipher.strip())
            if plain is None:
                plain = cipher.strip()
            keys.append((f"{service}:{name.strip()}", plain))
        else:
            keys.append((f"{service}:key-{len(keys)}", item.strip()))
    return keys


class SearchProxy:
    """MCP search server with per-provider key rotation. Exposes separate tools + rotation."""

    def __init__(self):
        self._rotators = {}
        for service in ("brave", "tavily", "youcom"):
            keys = _load_keys_for(service)
            if keys:
                self._rotators[service] = KeyRotator(
                    keys, state_file=os.path.join(STATE_DIR, f"{service}_state.json")
                )

        if not self._rotators:
            print("[search_proxy] No search API keys found", file=sys.stderr)
            return

        # you.com: preload with high calls → natural fallback
        you_rot = self._rotators.get("youcom")
        if you_rot:
            for i in range(you_rot.n):
                if you_rot._calls.get(i, 0) < YOUCOM_CALLS_PRELOAD:
                    you_rot._calls[i] = YOUCOM_CALLS_PRELOAD
            you_rot._save_state()

        _log(f"{sum(len(r.keys) for r in self._rotators.values())} keys loaded "
             f"(brave={len(self._rotators.get('brave',{}).keys or [])}, "
             f"tavily={len(self._rotators.get('tavily',{}).keys or [])}, "
             f"youcom={len(self._rotators.get('youcom',{}).keys or [])})")

    def _call_api(self, provider: str, key: str, query: str, max_results: int = 5) -> dict:
        cfg = PROVIDERS[provider]
        url = cfg["search_url"]
        headers = {}

        if "auth_key_field" in cfg:
            body_str = cfg["body"](query, max_results, key)
            headers.update(cfg.get("headers", {}))
        elif "auth_header" in cfg:
            headers[cfg["auth_header"]] = key
            headers.update(cfg.get("headers", {}))
            body_str = cfg.get("body", lambda q, n: None)(query, max_results) if "body" in cfg else None
        else:
            body_str = None

        if cfg["method"] == "GET":
            params = cfg.get("params", lambda q, n: {"q": q})(query, max_results)
            qs = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"
            body_bytes = None
        else:
            body_bytes = body_str.encode() if body_str else None

        req = urllib.request.Request(url, data=body_bytes, headers=headers, method=cfg["method"])
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            return {"status": resp.status, "data": json.loads(resp.read())}
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            return {"status": e.code, "error": body[:500], "data": None}
        except Exception as e:
            return {"status": 0, "error": str(e)[:500], "data": None}

    def _search_provider(self, provider: str, query: str, max_results: int = 5) -> list[str]:
        """Search using a single provider with its own key rotation."""
        rotator = self._rotators.get(provider)
        if rotator is None:
            return [f"error: {provider} not configured"]

        attempts = 0
        while attempts < rotator.n:
            attempts += 1
            picked = rotator.pick()
            if picked is None:
                break

            idx, full_name, key = picked
            _log(f"{full_name} → {provider}  (query: {query[:40]}...)")

            resp = self._call_api(provider, key, query, max_results)

            if resp["status"] in (200, 201):
                rotator.success(idx)
                rotator._save_state()
                results = self._format_text(provider, full_name, resp["data"])
                return results
            elif resp["status"] == 429:
                rotator.rate_limited(idx, 60)
                rotator._save_state()
                continue
            else:
                _log(f"{full_name} → {resp['status']}: {resp.get('error','?')[:100]}")
                rotator.rate_limited(idx, 30)
                rotator._save_state()
                continue

        return [f"error: {provider} exhausted"]

    def search(self, query: str, max_results: int = 5) -> list[str]:
        """Search with rotation across all Tier 1 providers, you.com as Tier 2 fallback."""
        for provider in ("brave", "tavily", "youcom"):
            result = self._search_provider(provider, query, max_results)
            if result and not result[0].startswith("error:"):
                if provider == "youcom":
                    _log("⚠️ you.com fallback activated (all Tier 1 keys exhausted)")
                    result = ["⚠️ you.com fallback (budget provider, $100/mo limit — use results sparingly)"] + result
                return result
        return ["error: All search providers exhausted"]

    _STRIP_TAGS_RE = re.compile(r"<[^>]*>")
    _STRIP_MD_RE = re.compile(r"\*{1,3}|_{1,3}|^#{1,4}\s?|^\s*[-*+]\s", re.MULTILINE)
    _COLLAPSE_WS_RE = re.compile(r"\s+")
    DESC_MAX_LEN = 150

    def _clean_text(self, text: str) -> str:
        """Remove HTML tags, markdown markers, and decode entities."""
        if not text:
            return ""
        text = self._STRIP_TAGS_RE.sub("", text)
        text = self._STRIP_MD_RE.sub("", text)
        text = html.unescape(text)
        text = self._COLLAPSE_WS_RE.sub(" ", text)
        return text.strip()

    def _format_results(self, provider: str, data: dict) -> list[dict]:
        """Normalize results from different providers into a common format."""
        if data is None:
            return []

        c = self._clean_text
        formatted = []
        if provider == "brave":
            for r in (data.get("web", {}).get("results", []) or []):
                formatted.append(
                    {
                        "title": c(r.get("title", "")),
                        "url": r.get("url", ""),
                        "description": c(r.get("description", "")),
                    }
                )
        elif provider == "tavily":
            for r in data.get("results", []) or []:
                formatted.append(
                    {
                        "title": c(r.get("title", "")),
                        "url": r.get("url", ""),
                        "description": c(r.get("content", "")),
                    }
                )
        elif provider == "youcom":
            for r in data.get("results", []) or data.get("hits", []) or []:
                formatted.append(
                    {
                        "title": c(r.get("title", "")),
                        "url": r.get("url", ""),
                        "description": c(r.get("description", "") or r.get("snippet", "")),
                    }
                )
        return formatted

    def _format_text(self, provider: str, key_name: str, data: dict) -> list[str]:
        lines = []
        formatted = self._format_results(provider, data)
        for r in formatted:
            title = r.get("title", "")
            url = r.get("url", "")
            desc = r.get("description", "")
            if len(desc) > self.DESC_MAX_LEN:
                desc = desc[:self.DESC_MAX_LEN] + "..."
            if not desc:
                desc = "-"
            lines.append(f"{title} | {url} | {desc}")
        return ["\n".join(lines)]

    def stats(self) -> str:
        return json.dumps(
            {name: rot.stats() for name, rot in self._rotators.items()},
            ensure_ascii=False,
        )


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
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "max_results": {
                    "type": "number",
                    "description": "Maximum number of results (default: 5)",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_stats",
        "description": "Show search proxy key rotation statistics per provider.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def _send(response: dict):
    """Write a JSON-RPC response to stdout."""
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
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "search-proxy",
                            "version": "1.0.0",
                        },
                    },
                }
            )
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = request.get("params", {})
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})

            if tool_name == "web_search":
                query = arguments.get("query", "")
                max_results = arguments.get("max_results", 5)
                results = proxy.search(query, max_results)
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": r} for r in results
                            ]
                        },
                    }
                )
            elif tool_name == "search_stats":
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": proxy.stats()}
                            ]
                        },
                    }
                )
            else:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                    }
                )
        elif method == "notifications/initialized":
            pass  # No response needed
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown method: {method}"},
                }
            )


if __name__ == "__main__":
    main()
