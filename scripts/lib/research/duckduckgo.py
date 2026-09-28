#!/usr/bin/env python3.12
# Status: experimental
# Path: imported by — lib/research/web.py (free fallback), lib/research/__init__.py
"""DuckDuckGo HTML search — key-less provider returning structured JSON results.

[WORKAROUND] DDG serves a CAPTCHA/anomaly page (HTTP 202, ~14 KB body) to datacenter
IPs, so requests go through the DataImpulse residential proxy. The exit IP is sticky
for 3h rather than rotating per request, so a retry only clears the CAPTCHA while the
anomaly is transient — not because the IP changed. Blocked responses are never read,
so a failed retry costs 0 B (only latency). The `html` endpoint is scraped; the JSON
`d.js` endpoint requires a vqd-token handshake, so it is not used.
"""

from __future__ import annotations

import gzip
import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Optional

from lib.research import proxy

HTML_ENDPOINT = "https://html.duckduckgo.com/html/"
LITE_ENDPOINT = "https://lite.duckduckgo.com/lite/"
REGION_DEFAULT = "wt-wt"
TIMEOUT = 20
DEFAULT_RETRIES = 2

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    # [WHY] an uncompressed SERP is ~26 KB; gzip brings it down to ~4.4 KB, and the
    # residential proxy bills by transferred GB.
    "Accept-Encoding": "gzip",
}
_WS_RE = re.compile(r"\s+")


def _gunzip(body: bytes, content_encoding: str) -> bytes:
    """Inflate a gzipped HTTP body, returning raw bytes when it is not gzip.

    A mislabeled or truncated body must never raise: returning it raw lets
    `parse_serp` yield 0 rows and take the existing retry path instead.
    """
    if not body or "gzip" not in (content_encoding or "").lower():
        return body
    try:
        return gzip.decompress(body)
    except Exception:  # noqa: BLE001 — degrade to "0 rows -> retry", never a traceback
        return body


def _log(msg: str) -> None:
    print(f"[research.duckduckgo] {msg}", file=sys.stderr, flush=True)


def _unwrap_url(href: str) -> str:
    """DDG wraps results as //duckduckgo.com/l/?uddg=<percent-encoded-url>."""
    if "uddg=" in href:
        return urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
    if href.startswith("//"):
        return "https:" + href
    return href


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def _is_title(cls: str) -> bool:
    return "result__a" in cls or "result-link" in cls


def _is_snippet(cls: str) -> bool:
    return "result__snippet" in cls or "result-snippet" in cls


class _SerpParser(HTMLParser):
    """Extract result rows (title/url/description) from a DDG html/lite SERP."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._field: Optional[str] = None
        self._tag: Optional[str] = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        href = a.get("href") or ""
        if _is_title(cls):
            self.results.append({"title": "", "url": _unwrap_url(href), "description": ""})
            self._field, self._tag, self._buf = "title", tag, []
        elif (
            self._field == "title"
            and tag == "a"
            and href
            and self.results
            and not self.results[-1]["url"]
        ):
            # lite puts `result-link` on a <td>; the anchor with href is nested.
            self.results[-1]["url"] = _unwrap_url(href)
        elif _is_snippet(cls) and self.results:
            self._field, self._tag, self._buf = "description", tag, []

    def handle_endtag(self, tag: str) -> None:
        if self._field is not None and tag == self._tag:
            self._commit()

    def handle_data(self, data: str) -> None:
        if self._field is not None:
            self._buf.append(data)

    def _commit(self) -> None:
        text = _clean("".join(self._buf))
        if self._field == "title" and self.results:
            self.results[-1]["title"] = text
        elif self._field == "description" and self.results:
            self.results[-1]["description"] = text
        self._field, self._tag, self._buf = None, None, []


def parse_serp(raw: bytes) -> list[dict[str, str]]:
    parser = _SerpParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    return [r for r in parser.results if r["url"] and r["title"]]


def _post(endpoint: str, query: str, region: str, proxy_url: Optional[str]) -> tuple[int, bytes]:
    data = urllib.parse.urlencode({"q": query, "kl": region}).encode()
    request = urllib.request.Request(endpoint, data=data, headers=_HEADERS)
    handlers = (
        [urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})] if proxy_url else []
    )
    try:
        with urllib.request.build_opener(*handlers).open(request, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                # [WHY] only status 200 is ever parsed, and a 202 anomaly page costs
                # ~14 KB uncompressed — 3x a good response. Downloading it is pure waste.
                return resp.status, b""
            body = _gunzip(resp.read(), resp.headers.get("Content-Encoding", ""))
            return resp.status, body
    except urllib.error.HTTPError as exc:
        # [WHY] same as above: a non-2xx body is never inspected, so don't fetch it.
        exc.close()
        return exc.code, b""
    except Exception as exc:  # noqa: BLE001 — surfaced as an empty result set
        return 0, str(exc).encode()


def duckduckgo_search(
    query: str,
    max_results: int = 5,
    region: str = REGION_DEFAULT,
    retries: int = DEFAULT_RETRIES,
) -> list[dict[str, str]]:
    """Return [{title,url,description,source}] or [] when blocked/unavailable."""
    if not query or not query.strip():
        return []
    proxy_url = proxy.proxy_url()
    # [WHY] the residential exit IP is sticky for 3h (not per-request), so a retry
    # clears the CAPTCHA only while the anomaly is transient rather than tied to the
    # IP; lite is the last resort. Non-200 bodies are never read, so a blocked retry
    # costs 0 B — just the request latency.
    endpoints = [HTML_ENDPOINT] * (retries + 1) + [LITE_ENDPOINT]
    for attempt, endpoint in enumerate(endpoints):
        status, body = _post(endpoint, query, region, proxy_url)
        rows = parse_serp(body) if status == 200 else []
        if rows:
            for row in rows:
                row["source"] = "duckduckgo"
            return rows[:max_results]
        _log(f"attempt {attempt + 1}/{len(endpoints)} status={status} rows=0 ({endpoint})")
        if attempt < len(endpoints) - 1:
            time.sleep(1.0 * (attempt + 1))
    return []
