#!/usr/bin/env python3
"""
Unified MCP search proxy with cross-provider key rotation.

SLOC-exempt: 448 lines — single cohesive search proxy (key rotation → provider
dispatch → API call → result formatting). MultiProviderRotator, per-provider
response parsers, and HTTP client share key state. Splitting would scatter
rotation logic across files.

Providers (Tier 1): Brave ×4 + Exa ×4 + Tavily ×4 = 12 keys, even rotation
Provider  (Tier 2): you.com ×4 — only when Tier 1 all in backoff

Protocol: MCP stdio (JSON-RPC over stdin/stdout).
Replaces search-mcp-wrapper + per-provider MCP servers.
"""

import html
import json
import os
import re
import time
import urllib.request
import urllib.error

from lib.key_rotator import KeyRotator
from lib.crypto import decrypt_data

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
    "exa": {
        "prefix": "EXA",
        "search_url": "https://api.exa.ai/search",
        "auth_header": "x-api-key",
        "max_results": 10,
        "method": "POST",
        "body": lambda q, n: json.dumps({"query": q, "numResults": n, "type": "auto"}),
        "headers": {"Content-Type": "application/json"},
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
STATE_FILE = os.path.join(STATE_DIR, "search_proxy_state.json")

# you.com: Tier 2 fallback — preload with high calls so they sort last
YOUCOM_CALLS_PRELOAD = 1_000_000


def _load_all_keys() -> list[tuple[str, str]]:
    """Load all search API keys from secrets, prefixed by provider."""
    secrets_path = os.path.expanduser("~/.config/devforge/secrets.env")
    if not os.path.exists(secrets_path):
        return []

    all_keys = []
    for provider, cfg in PROVIDERS.items():
        env_name = f"{cfg['prefix']}_API_KEYS"
        keys_str = ""
        with open(secrets_path) as f:
            for line in f:
                if line.startswith(f"{env_name}="):
                    keys_str = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if not keys_str:
            continue

        for item in keys_str.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                name, cipher = item.split(":", 1)
                plain = decrypt_data(cipher.strip())
                if plain is None:
                    plain = cipher.strip()
                # Prefix key name with provider for routing
                all_keys.append((f"{provider}:{name.strip()}", plain))
            else:
                all_keys.append((f"{provider}:key-{len(all_keys)}", item.strip()))
    return all_keys


def _get_provider(key_name: str) -> str:
    """Extract provider from key name prefix. 'brave:mesids_...' → 'brave'"""
    return key_name.split(":", 1)[0]


def _get_key_name(key_name: str) -> str:
    """Extract bare key name without provider prefix."""
    return key_name.split(":", 1)[1] if ":" in key_name else key_name


class SearchProxy:
    """Unified MCP search server with cross-provider key rotation."""

    def __init__(self):
        keys = _load_all_keys()
        if not keys:
            print("[search_proxy] No search API keys found", file=sys.stderr)
            self._rotator = None
            return

        self._rotator = KeyRotator(keys, state_file=STATE_FILE)

        # Preload you.com keys with high call counts → sort last
        for i, (name, _key) in enumerate(keys):
            if name.startswith("youcom:"):
                if self._rotator._calls.get(i, 0) < YOUCOM_CALLS_PRELOAD:
                    self._rotator._calls[i] = YOUCOM_CALLS_PRELOAD

        self._rotator._save_state()
        print(
            f"[search_proxy] {len(keys)} keys loaded "
            f"({sum(1 for n,_ in keys if not n.startswith('youcom:'))} Tier1 + "
            f"{sum(1 for n,_ in keys if n.startswith('youcom:'))} Tier2)",
            file=sys.stderr,
        )

    def _call_api(self, provider: str, key: str, query: str, max_results: int = 5) -> dict:
        """Call a search provider's REST API."""
        cfg = PROVIDERS[provider]
        url = cfg["search_url"]
        headers = {}

        if "auth_key_field" in cfg:
            # API key in body (Tavily) — body builder needs the key
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

    def search(self, query: str, max_results: int = 5) -> list[str]:
        """Execute a search with automatic provider fallback."""
        if self._rotator is None:
            return ["error: No search keys available"]

        attempts = 0
        max_attempts = self._rotator.n

        while attempts < max_attempts:
            attempts += 1
            picked = self._rotator.pick()
            if picked is None:
                break

            idx, full_name, key = picked
            provider = _get_provider(full_name)
            bare_name = _get_key_name(full_name)

            print(
                f"[search_proxy] {full_name} → {provider}  (query: {query[:40]}...)",
                file=sys.stderr,
            )

            resp = self._call_api(provider, key, query, max_results)

            if resp["status"] in (200, 201):
                self._rotator.success(idx)
                self._rotator._save_state()
                return self._format_text(provider, bare_name, resp["data"])
            elif resp["status"] == 429:
                print(
                    f"[search_proxy] {full_name} → 429, backoff 60s",
                    file=sys.stderr,
                )
                self._rotator.rate_limited(idx, 60)
                self._rotator._save_state()
                continue
            else:
                print(
                    f"[search_proxy] {full_name} → {resp['status']}: {resp.get('error','?')[:100]}",
                    file=sys.stderr,
                )
                self._rotator.rate_limited(idx, 30)
                self._rotator._save_state()
                continue

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
        elif provider == "exa":
            for r in data.get("results", []) or []:
                formatted.append(
                    {
                        "title": c(r.get("title", "")),
                        "url": r.get("url", ""),
                        "description": c(r.get("text", "") or r.get("highlights", [""])[0]),
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
        """Format results as clean concise text.

        [provider:key]
        title | url | description (truncated)
        ...
        """
        lines = [f"[{provider}:{key_name}]"]
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
        if self._rotator is None:
            return json.dumps({"error": "No keys"})
        return json.dumps(self._rotator.stats(), ensure_ascii=False)


TOOLS = [
    {
        "name": "web_search",
        "description": (
            "Search the web using a unified pool of search engines (Brave, Exa, Tavily, you.com). "
            "Keys rotate evenly across providers. On 429, automatically falls back to the next provider. "
            "you.com is used only as a last resort when all other providers are exhausted."
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
        "description": "Show search proxy key rotation statistics.",
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
