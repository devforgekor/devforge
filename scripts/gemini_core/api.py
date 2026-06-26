#!/usr/bin/env python3.11
# Status: production
"""Gemini API call wrapper."""

import json, ssl, time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

API_BASE = "https://generativelanguage.googleapis.com:4430/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-flash"

_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


def call_gemini(contents, tools=None):
    """Single round-trip to Gemini API. Returns response dict or None on failure."""
    body = {"contents": contents}
    if tools:
        body["tools"] = tools
    data = json.dumps(body).encode()
    url = f"{API_BASE}/{DEFAULT_MODEL}:generateContent"
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urlopen(req, context=_CTX, timeout=120) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 502 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            err_body = e.read().decode(errors="replace")
            return {"error": f"API error {e.code}: {err_body[:300]}"}
        except URLError as e:
            return {"error": f"Network error: {e.reason}"}
    return {"error": "502 after 3 retries"}
