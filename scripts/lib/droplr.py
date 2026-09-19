#!/usr/bin/env python3.11
# Status: production
# Path: imported by — blob_explorer/blob.py, lib
"""Droplr link shortener via HTTP API — no Node CLI dependency.

API: POST https://api.droplr.com/links  (Content-Type: text/plain, body = URL)
Returns the created drop's `shortlink`, or None on any failure.

Auth priority: Bearer token (DROPLR_AUTH_TOKEN) > Basic (DROPLR_USER / DROPLR_PASS).
Bearer token auto-rotates when expired via Basic auth at /token endpoint.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request
from typing import Optional

API = "https://api.droplr.com"

_bearer_token: Optional[str] = None


def _cred(name: str) -> str:
    return os.environ.get(name, "")


def _decode_jwt_exp(token: str) -> Optional[float]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return float(data.get("exp", 0))
    except Exception:
        return None


def _is_token_valid(token: str, buffer_sec: int = 86400) -> bool:
    if not token:
        return False
    exp = _decode_jwt_exp(token)
    if not exp:
        return True
    return time.time() < exp - buffer_sec


def _refresh_bearer_token(timeout: int = 20) -> Optional[str]:
    email = _cred("DROPLR_USER")
    pw = _cred("DROPLR_PASS")
    if not email or not pw:
        return None
    basic = base64.b64encode(f"{email}:{pw}".encode()).decode()
    for url in (API + "/token", API + "/oauth/token"):
        try:
            req = urllib.request.Request(
                url, method="POST", headers={"Authorization": "Basic " + basic},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
            if "token" in data:
                return data["token"]
        except Exception:
            continue
    return None


def _get_bearer_token(timeout: int = 20) -> Optional[str]:
    global _bearer_token
    if _bearer_token and _is_token_valid(_bearer_token):
        return _bearer_token
    new_token = _refresh_bearer_token(timeout)
    if new_token:
        _bearer_token = new_token
        return new_token
    return None


def shorten(url: str, timeout: int = 20) -> Optional[str]:
    token_val = _get_bearer_token(timeout)
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
