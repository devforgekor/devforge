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
    """Return True if a session cookie is present.

    Sessions are cookie-presence based: there is no server-side session store,
    so this only asserts that the browser completed the login flow. The previous
    implementation ended with `... or True`, making it always-True — a latent
    auth bypass that would have authorized every request regardless of cookie.
    The secure fix is a server-side/signed session (tracked separately); this
    change removes the unconditional bypass and matches require_auth().
    """
    return bool(request.cookies.get(COOKIE_NAME))


def require_auth(request: Request) -> Optional[RedirectResponse]:
    """Return None if authenticated, else redirect to /login."""
    if not request.cookies.get(COOKIE_NAME):
        return RedirectResponse("/login", status_code=302)
    return None
