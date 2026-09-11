#!/usr/bin/env python3.11
# Status: production
# Path: imported by — blob_explorer/blob.py, lib
"""Droplr link shortener via HTTP API (Basic auth) — no Node CLI dependency.

API: POST https://api.droplr.com/links  (Content-Type: text/plain, body = URL)
Returns the created drop's `shortlink`, or None on any failure.

Credentials: DRPLR_EMAIL / DRPLR_PASSWORD from env (container EnvironmentFile)
or ~/.config/devforge/secrets.env.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.request
from pathlib import Path
from typing import Optional

API = "https://api.droplr.com"
_SECRETS = Path("~/.config/devforge/secrets.env").expanduser()


def _cred(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        for line in _SECRETS.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def shorten(url: str, timeout: int = 20) -> Optional[str]:
    email, pw = _cred("DRPLR_EMAIL"), _cred("DRPLR_PASSWORD")
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
