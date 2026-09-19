#!/usr/bin/env python3.11
# Status: production
# Path: imported by — blob_explorer/blob.py, lib
"""Droplr link shortener via HTTP API — no Node CLI dependency.

API: POST https://api.droplr.com/links  (Content-Type: text/plain, body = URL)
Returns the created drop's `shortlink`, or None on any failure.

Auth priority: Bearer token (DROPLR_AUTH_TOKEN) > Basic (DROPLR_USER / DROPLR_PASS).
Basic auth requires DROPLR_USER/DROPLR_PASS via env (from Azure KV → systemd EnvironmentFile).
Bearer auth (DROPLR_AUTH_TOKEN) is optional — requires rotation logic before production use.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

API = "https://api.droplr.com"


def _cred(name: str) -> str:
    return os.environ.get(name, "")


def shorten(url: str, timeout: int = 20) -> Optional[str]:
    token_val = _cred("DROPLR_AUTH_TOKEN")
    if token_val:
        req = urllib.request.Request(
            API + "/links",
            data=url.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "text/plain", "Authorization": "Bearer " + token_val},
        )
    else:
        email, pw = _cred("DROPLR_USER"), _cred("DROPLR_PASS")
        if not email or not pw:
            return None
        token = base64.b64encode(f"{email}:{pw}".encode()).decode()
        req = urllib.request.Request(
            API + "/links",
            data=url.encode("utf-8"),
            method="POST",
            headers={"Content-Type": "text/plain", "Authorization": "Basic " + token},
        )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode())
        return data.get("shortlink") or data.get("link") or data.get("url")
    except Exception:
        return None
