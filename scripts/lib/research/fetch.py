#!/usr/bin/env python3.11
# Status: production
# Path: imported by — lib/research/__init__.py, cli.py
"""URL fetch core — retrieve page text (extracted capability; replaces fetch MCP)."""

from __future__ import annotations

import re
import urllib.request
import urllib.error

_STRIP_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_STRIP_TAGS = re.compile(r"<[^>]+>")
_COLLAPSE_WS = re.compile(r"[ \t\r\f\v]+")
_MULTI_NL = re.compile(r"\n\s*\n\s*\n+")
_UA = "Mozilla/5.0 (compatible; DevForgeResearch/1.0)"


def fetch_url(url: str, max_chars: int = 50000, timeout: int = 20) -> dict:
    """Fetch a URL and return {url, status, content_type, text, truncated}."""
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("url must be http(s)")
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "text/html,application/json,text/plain,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
            ctype = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "content_type": "", "text": "", "truncated": False, "error": str(e)[:200]}
    except Exception as e:
        return {"url": url, "status": 0, "content_type": "", "text": "", "truncated": False, "error": str(e)[:200]}

    text = raw.decode("utf-8", errors="replace")
    if "html" in ctype.lower() or text.lstrip()[:1] == "<":
        text = _STRIP_SCRIPT_STYLE.sub(" ", text)
        text = _STRIP_TAGS.sub(" ", text)
        text = _COLLAPSE_WS.sub(" ", text)
        text = _MULTI_NL.sub("\n\n", text)
    text = text.strip()
    truncated = len(text) > max_chars
    return {"url": url, "status": status, "content_type": ctype,
            "text": text[:max_chars], "truncated": truncated}
