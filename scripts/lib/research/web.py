#!/usr/bin/env python3.11
# Status: production
# Path: imported by — proxies/search.py, lib/research/__init__.py, cli.py
"""Web search core — Brave → Tavily → you.com rotation (extracted from proxies/search.py).

Key rotation uses lib.auth.key_rotator.KeyRotator with state under ~/.cache/devforge.
Exposes structured results (list[dict]) plus text helpers for MCP parity.
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from lib.auth.key_loader import load_api_keys
from lib.auth.key_rotator import KeyRotator

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
        "prefix": "TAVILY",
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
YOUCOM_CALLS_PRELOAD = 1_000_000


def _log(msg: str):
    print(f"[research.web] {msg}", file=sys.stderr, flush=True)


def _load_keys_for(service: str) -> list[tuple[str, str]]:
    """Round-robin-ready keys for a search provider.

    Delegates to the shared loader (lib.auth.key_loader.load_api_keys), which
    handles both consolidated (PREFIX_API_KEYS) and per-account
    (PREFIX_ACCOUNT_API_KEY) env formats for every rotating provider.
    """
    cfg = PROVIDERS.get(service)
    if not cfg:
        return []
    return load_api_keys(cfg["prefix"], service=service)


class SearchProxy:
    """Per-provider key rotation across Brave/Tavily/you.com."""

    _STRIP_TAGS_RE = re.compile(r"<[^>]*>")
    _STRIP_MD_RE = re.compile(r"\*{1,3}|_{1,3}|^#{1,4}\s?|^\s*[-*+]\s", re.MULTILINE)
    _COLLAPSE_WS_RE = re.compile(r"\s+")
    DESC_MAX_LEN = 150

    def __init__(self):
        self._rotators = {}
        for service in ("brave", "tavily", "youcom"):
            keys = _load_keys_for(service)
            if keys:
                self._rotators[service] = KeyRotator(
                    keys, state_file=os.path.join(STATE_DIR, f"{service}_state.json")
                )

        if not self._rotators:
            _log("No search API keys found")
            return

        you_rot = self._rotators.get("youcom")
        if you_rot:
            for i in range(you_rot.n):
                if you_rot._calls.get(i, 0) < YOUCOM_CALLS_PRELOAD:
                    you_rot._calls[i] = YOUCOM_CALLS_PRELOAD
            you_rot._save_state()

    @property
    def available(self) -> bool:
        return bool(self._rotators)

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
            body_str = (
                cfg.get("body", lambda q, n: None)(query, max_results) if "body" in cfg else None
            )
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
                return self._format_text(provider, full_name, resp["data"])
            elif resp["status"] == 429:
                rotator.rate_limited(idx, 60)
                rotator._save_state()
                continue
            else:
                _log(f"{full_name} → {resp['status']}: {resp.get('error', '?')[:100]}")
                rotator.rate_limited(idx, 30)
                rotator._save_state()
                continue

        return [f"error: {provider} exhausted"]

    def search(self, query: str, max_results: int = 5) -> list[str]:
        """Text lines. Brave → Tavily → you.com fallback."""
        for provider in ("brave", "tavily", "youcom"):
            result = self._search_provider(provider, query, max_results)
            if result and not result[0].startswith("error:"):
                if provider == "youcom":
                    _log("you.com fallback activated")
                    result = ["(you.com fallback — budget provider)"] + result
                return result
        return ["error: All search providers exhausted"]

    def search_structured(self, query: str, max_results: int = 5) -> list[dict]:
        """Structured results across providers: [{title,url,description,source}]."""
        for provider in ("brave", "tavily", "youcom"):
            rotator = self._rotators.get(provider)
            if rotator is None:
                continue
            attempts = 0
            while attempts < rotator.n:
                attempts += 1
                picked = rotator.pick()
                if picked is None:
                    break
                idx, full_name, key = picked
                resp = self._call_api(provider, key, query, max_results)
                if resp["status"] in (200, 201):
                    rotator.success(idx)
                    rotator._save_state()
                    out = self._format_results(provider, resp["data"])
                    for r in out:
                        r["source"] = provider
                    return out
                else:
                    if resp["status"] == 429:
                        rotator.rate_limited(idx, 60)
                    else:
                        rotator.rate_limited(idx, 30)
                    rotator._save_state()
                    continue
        return []

    def _clean_text(self, text: str) -> str:
        if not text:
            return ""
        text = self._STRIP_TAGS_RE.sub("", text)
        text = self._STRIP_MD_RE.sub("", text)
        text = html.unescape(text)
        text = self._COLLAPSE_WS_RE.sub(" ", text)
        return text.strip()

    def _format_results(self, provider: str, data: dict) -> list[dict]:
        if data is None:
            return []
        c = self._clean_text
        formatted = []
        if provider == "brave":
            for r in data.get("web", {}).get("results", []) or []:
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
        for r in self._format_results(provider, data):
            title, url, desc = r.get("title", ""), r.get("url", ""), r.get("description", "")
            if len(desc) > self.DESC_MAX_LEN:
                desc = desc[: self.DESC_MAX_LEN] + "..."
            lines.append(f"{title} | {url} | {desc or '-'}")
        return ["\n".join(lines)]

    def stats(self) -> str:
        return json.dumps(
            {name: rot.stats() for name, rot in self._rotators.items()}, ensure_ascii=False
        )


_proxy: SearchProxy | None = None


def get_proxy() -> SearchProxy:
    global _proxy
    if _proxy is None:
        _proxy = SearchProxy()
    return _proxy


def web_search(query: str, max_results: int = 5) -> list[str]:
    """Text lines (MCP parity)."""
    return get_proxy().search(query, max_results)


def web_search_structured(query: str, max_results: int = 5) -> list[dict]:
    return get_proxy().search_structured(query, max_results)


def search_stats() -> str:
    return get_proxy().stats()
