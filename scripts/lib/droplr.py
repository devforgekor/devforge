#!/usr/bin/env python3.11
# Status: production
# Path: imported by — blob_explorer/blob.py, lib
"""Droplr link shortener via HTTP API — no Node CLI dependency.

API: POST https://api.droplr.com/links  (Content-Type: text/plain, body = URL)
Returns the created drop's `shortlink`, or None on any failure.

Auth: HTTP Basic (DROPLR_USER / DROPLR_PASS). Droplr exposes no public token
issuance/refresh API (verified 2026-09-20: /token, /oauth/token → 404; auth host
WAF-blocked), so the short-lived bearer JWT path was removed for stable Basic auth.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from typing import Optional

API = "https://api.droplr.com"


def _cred(name: str) -> str:
    return os.environ.get(name, "")


def _basic_auth() -> Optional[str]:
    email = _cred("DROPLR_USER")
    pw = _cred("DROPLR_PASS")
    if not email or not pw:
        return None
    return base64.b64encode(f"{email}:{pw}".encode()).decode()


def shorten(url: str, timeout: int = 20) -> Optional[str]:
    auth = _basic_auth()
    if not auth:
        return None
    req = urllib.request.Request(
        API + "/links",
        data=url.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "text/plain", "Authorization": "Basic " + auth},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return data.get("shortlink") or data.get("link") or data.get("url")
    except Exception:
        return None
