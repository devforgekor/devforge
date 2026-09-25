#!/usr/bin/env python3.11
# Status: production
# Path: imported by — context7_mcp.py, lib/research/__init__.py, cli.py
"""Context7 docs core — library resolve + docs query (extracted from context7_mcp.py).

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

API_BASE = "https://context7.com/api"
STATE_FILE = os.path.expanduser("~/.cache/devforge/context7_state.json")
# [WHY] 429 재시도:Retry-After가 없을 때 10s — KeyRotator의 일일쿼타 임계(300s)보다
# 작아야 짧은 지터백오프로 키를 넘기는 동작이 유지된다.
_RETRY_SECONDS = 10

_rotator: KeyRotator | None = None


def _get_rotator() -> KeyRotator | None:
    global _rotator
    if _rotator is None:
        keys = load_api_keys("CONTEXT7")
        if keys:
            _rotator = KeyRotator(keys, state_file=STATE_FILE)
    return _rotator


def available() -> bool:
    return _get_rotator() is not None


def _retry_after(resp: httpx.Response) -> int:
    try:
        return max(1, int(resp.headers.get("Retry-After", _RETRY_SECONDS)))
    except ValueError:
        return _RETRY_SECONDS


def _call_api(path: str, params: dict, method: str = "GET") -> dict | None:
    rot = _get_rotator()
    if rot is None:
        return None
    for _ in range(rot.n):
        picked = rot.pick()
        if picked is None:
            return None
        idx, _name, key = picked
        try:
            with httpx.Client(timeout=20.0) as client:
                if method == "GET":
                    resp = client.get(
                        f"{API_BASE}{path}", params=params, headers={"x-api-key": key}
                    )
                else:
                    resp = client.post(f"{API_BASE}{path}", json=params, headers={"x-api-key": key})
                if resp.status_code == 429:
                    rot.rate_limited(idx, retry_seconds=_retry_after(resp))
                    continue
                resp.raise_for_status()
                rot.success(idx)
                ct = resp.headers.get("content-type", "")
                if ct.startswith("application/json"):
                    return resp.json()
                return {"data": resp.text}
        except Exception as e:
            print(f"[research.context7] Key {idx} failed: {e}", file=sys.stderr)
            continue
    return None


def resolve_library_id(query: str, library_name: str) -> list[dict]:
    """Return Context7 library matches (structured). Empty list if none/unavailable."""
    library_name = (library_name or "").strip()
    if not library_name:
        raise ValueError("libraryName is required")
    data = _call_api(
        "/v2/libs/search", {"query": (query or library_name), "libraryName": library_name}
    )
    if data is None:
        return []
    return data.get("results", []) or []


def query_docs(library_id: str, query: str) -> dict:
    """Return raw Context7 context payload. Empty dict if unavailable."""
    library_id = (library_id or "").strip()
    if not library_id:
        raise ValueError("libraryId is required")
    data = _call_api(
        "/v2/context", {"libraryId": library_id, "query": query or "general usage", "type": "json"}
    )
    return data or {}


def format_resolve_text(library_name: str, results: list[dict]) -> str:
    if not results:
        return f"No libraries found matching '{library_name}'."
    lines = [f"Available libraries for '{library_name}':\n"]
    for r in results:
        lines.append(f"- {r.get('title', 'Untitled')}")
        lines.append(f"  ID: {r.get('id', 'N/A')}")
        lines.append(f"  Description: {r.get('description', '')[:200]}")
        lines.append(
            f"  Snippets: {r.get('totalSnippets', 0)} | Reputation: {r.get('trustScore', 'N/A')} | Score: {r.get('benchmarkScore', 'N/A')}"
        )
        versions = r.get("versions", [])
        if versions:
            lines.append(
                f"  Versions: {', '.join(versions[:5])}{'...' if len(versions) > 5 else ''}"
            )
        lines.append("")
    return "\n".join(lines)


def format_docs_text(library_id: str, data: dict) -> str:
    snippets = data.get("codeSnippets", []) or []
    info = data.get("infoSnippets", []) or []
    parts = []
    for s in info:
        parts.append(f"### {s.get('breadcrumb', 'Documentation')}")
        parts.append(s.get("content", ""))
        if s.get("pageId"):
            parts.append(f"Source: {s['pageId']}")
        parts.append("")
    for s in snippets:
        parts.append(f"### {s.get('codeTitle', 'Code Example')}")
        if s.get("codeDescription"):
            parts.append(s["codeDescription"])
        for c in s.get("codeList", []):
            lang, code = c.get("language", ""), c.get("code", "")
            if lang and code:
                parts.append(f"```{lang}\n{code}\n```")
            elif code:
                parts.append(f"```\n{code}\n```")
        if s.get("pageTitle"):
            parts.append(f"Source: {s['pageTitle']}")
        parts.append("")
    text = "\n".join(parts).strip()
    if not text:
        return f"No documentation found for library ID '{library_id}'."
    return text[:10000] + ("..." if len(text) > 10000 else "")


def resolve_library_id_text(query: str, library_name: str) -> str:
    try:
        results = resolve_library_id(query, library_name)
    except ValueError as e:
        return f"Error: {e}"
    if not results and not available():
        return "Error: All Context7 API keys exhausted or API unavailable."
    return format_resolve_text(library_name, results)


def query_docs_text(library_id: str, query: str) -> str:
    try:
        data = query_docs(library_id, query)
    except ValueError as e:
        return f"Error: {e}"
    if not data:
        return "Error: All Context7 API keys exhausted or API unavailable."
    return format_docs_text(library_id, data)
