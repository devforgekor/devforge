#!/usr/bin/env python3.11
# Status: production
# Path: imported by — exa_mcp.py, lib/research/__init__.py, cli.py
"""Exa search core — semantic web search + contents (extracted from exa_mcp.py).

Key rotation uses lib.auth.key_rotator.KeyRotator with state under ~/.cache/devforge.
"""

from __future__ import annotations

import os
import sys

_SCRIPTS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import httpx  # noqa: E402

from lib.auth.key_loader import load_api_keys  # noqa: E402
from lib.auth.key_rotator import KeyRotator  # noqa: E402

DEFAULT_NUM = 10
MAX_NUM = 100
API_BASE = "https://api.exa.ai"
STATE_FILE = os.path.expanduser("~/.cache/devforge/exa_state.json")
# [WHY] 429 재시도 기본값 — KeyRotator 일일쿼타 임계(300s) 미만이어야 짧은 지터백오프.
_RETRY_SECONDS = 10

_rotator: KeyRotator | None = None


def _get_rotator() -> KeyRotator | None:
    global _rotator
    if _rotator is None:
        keys = load_api_keys("EXA")
        if keys:
            _rotator = KeyRotator(keys, state_file=STATE_FILE)
    return _rotator


def available() -> bool:
    return _get_rotator() is not None


def _call_api(key: str, endpoint: str, payload: dict) -> dict:
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{API_BASE}{endpoint}", headers=headers, json=payload)
        if resp.status_code == 429:
            return {"_rate_limited": True, "status": 429}
        resp.raise_for_status()
        return resp.json()


def _call_rotated(endpoint: str, payload: dict) -> dict:
    """Pick a key via the shared KeyRotator and call once. Raises if every key fails."""
    rot = _get_rotator()
    if rot is None:
        raise RuntimeError("EXA API keys not configured")
    last_error = ""
    for _ in range(rot.n):
        picked = rot.pick()
        if picked is None:
            break
        idx, _name, key = picked
        try:
            result = _call_api(key, endpoint, payload)
        except Exception as e:
            last_error = str(e)
            print(f"[research.exa] Key {idx} failed: {e}", file=sys.stderr)
            continue
        if isinstance(result, dict) and result.get("_rate_limited"):
            rot.rate_limited(idx, retry_seconds=_RETRY_SECONDS)
            continue
        rot.success(idx)
        return result
    if last_error:
        raise RuntimeError(f"All {rot.n} Exa API keys failed: {last_error}")
    raise RuntimeError(f"All {rot.n} Exa API keys rate-limited")


def exa_search(
    query: str,
    type: str = "auto",
    num_results: int = DEFAULT_NUM,
    include_domains=None,
    exclude_domains=None,
    category=None,
    start_published_date=None,
    end_published_date=None,
    text: bool = False,
    highlights: bool = True,
    summary: bool = False,
) -> list[dict]:
    """Return raw Exa result dicts. Raises RuntimeError if all keys rate-limited."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query is required")

    payload = {"query": query, "type": type, "numResults": min(num_results, MAX_NUM)}
    if include_domains:
        payload["includeDomains"] = include_domains
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains
    if category:
        payload["category"] = category
    if start_published_date:
        payload["startPublishedDate"] = start_published_date
    if end_published_date:
        payload["endPublishedDate"] = end_published_date
    contents = {}
    if text:
        contents["text"] = True
    if highlights:
        contents["highlights"] = True
    if summary:
        contents["summary"] = {}
    if contents:
        payload["contents"] = contents

    return _call_rotated("/search", payload).get("results", []) or []


def exa_contents(urls: list[str], text: bool = True, highlights: bool = True) -> list[dict]:
    """Return raw Exa contents result dicts. Raises RuntimeError if all keys rate-limited."""
    if not urls:
        raise ValueError("urls is required")

    payload: dict = {"urls": urls[:10]}
    contents = {}
    if text:
        contents["text"] = {"maxCharacters": 5000}
    if highlights:
        contents["highlights"] = True
    if contents:
        payload["contents"] = contents

    return _call_rotated("/contents", payload).get("results", []) or []


# ── text helpers (MCP parity) ─────────────────────────────────


def format_search_text(query: str, results: list[dict], payload_type: str = "auto") -> str:
    lines = []
    for r in results:
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        published = r.get("publishedDate", "")
        preview = ""
        if r.get("highlights"):
            preview = "\n    Highlights: " + " | ".join(r["highlights"][:2])
        elif r.get("text"):
            t = r["text"]
            preview = "\n    Text: " + (t[:200] + "..." if len(t) > 200 else t)
        elif r.get("summary"):
            preview = "\n    Summary: " + r["summary"][:200]
        lines.append(f"- {title}\n  URL: {url}\n  Published: {published}{preview}")
    meta = f"Search results for: {query} (type={payload_type})"
    return meta + "\n\n" + "\n\n".join(lines) if lines else meta + "\n\nNo results found."


def format_contents_text(results: list[dict]) -> str:
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


def exa_search_text(query: str, **opts) -> str:
    type_ = opts.get("type", "auto")
    try:
        results = exa_search(query, **opts)
    except RuntimeError as e:
        return f"Error: {e}"
    return format_search_text(query, results, type_)


def exa_contents_text(urls: list[str], **opts) -> str:
    try:
        results = exa_contents(urls, **opts)
    except RuntimeError as e:
        return f"Error: {e}"
    return format_contents_text(results)
