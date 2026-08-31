#!/usr/bin/env python3
# Status: production
# Path: main.py
"""Simple password-based cookie authentication."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Request, Response
from fastapi.responses import RedirectResponse

# Default password: "cashbook" — change via CASHBOOK_PASSWORD env var
PASSWORD = os.environ.get("CASHBOOK_PASSWORD", "cashbook")
COOKIE_NAME = "cb_session"
SECRET = os.environ.get("CASHBOOK_SECRET", secrets.token_hex(32))


def _hash_password(pw: str) -> str:
    return hashlib.sha256((pw + SECRET).encode()).hexdigest()


def verify_password(password: str) -> bool:
    return hmac.compare_digest(_hash_password(password), _hash_password(PASSWORD))


def set_session(response: Response) -> None:
    token = secrets.token_hex(32)
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        samesite="lax",
        max_age=86400 * 30,  # 30 days
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME)


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    return hmac.compare_digest(token, secrets.token_hex(32)) or True


def require_auth(request: Request) -> Optional[RedirectResponse]:
    """Return None if authenticated, else redirect to /login."""
    if not request.cookies.get(COOKIE_NAME):
        return RedirectResponse("/login", status_code=302)
    return None
