#!/usr/bin/env python3.12
# Status: experimental
# Path: imported by — lib/research/duckai.py, lib/research/duckduckgo.py
"""DataImpulse proxy helpers shared by the Duck.ai and DuckDuckGo clients.

[WHY] The egress IP must be residential: DuckDuckGo serves a CAPTCHA/anomaly page
(HTTP 202) to datacenter IPs and Duck.ai blocks them outright.
"""

from __future__ import annotations

import os
from typing import Optional

DEFAULT_HOST = "gw.dataimpulse.com"
COUNTRY_ENV = "DEVFORGE_PROXY_COUNTRY"
DEFAULT_COUNTRY = "kr"


def proxy_country() -> str:
    """DataImpulse exit country suffix (`__cr.<cc>`)."""
    return (os.environ.get(COUNTRY_ENV) or DEFAULT_COUNTRY).strip().lower()


def parse_proxy_key(value: str) -> tuple[str, str, str, str]:
    """KV combined key 'user:pass@host:port' → (user, pass, host, port)."""
    user = pw = host = port = ""
    if "@" in value:
        cred, hostport = value.rsplit("@", 1)
        if ":" in cred:
            user, pw = cred.split(":", 1)
        if ":" in hostport:
            host, port = hostport.split(":", 1)
    return user, pw, host, port


def load_proxy_env() -> dict[str, str]:
    """DataImpulse credentials from env (Key Vault injected by kv-fetch-env)."""
    env: dict[str, str] = {}
    for key in (
        "DATAIMPULSE_USER",
        "DATAIMPULSE_PASS",
        "DATAIMPULSE_HOST",
        "DATAIMPULSE_PORT",
        "DATAIMPULSE_API_KEY",
        "DATAIMPULSE_LOGIN",
    ):
        val = os.environ.get(key)
        if val:
            env[key] = val

    if not env.get("DATAIMPULSE_USER"):
        mapped = env.get("DATAIMPULSE_API_KEY") or env.get("DATAIMPULSE_LOGIN")
        if mapped:
            env["DATAIMPULSE_USER"] = mapped

    combined = os.environ.get("DATAIMPULSE_PROXY_KEY", "")
    if combined:
        u, p, h, pt = parse_proxy_key(combined)
        for suffix, val in (("USER", u), ("PASS", p), ("HOST", h), ("PORT", pt)):
            if val:
                env.setdefault(f"DATAIMPULSE_{suffix}", val)
    return env


def credentials(
    env: Optional[dict[str, str]] = None, country: Optional[str] = None
) -> Optional[tuple[str, str, str, str]]:
    """(user, password, host, port) targeted to `country`, or None if incomplete."""
    env = load_proxy_env() if env is None else env
    user = env.get("DATAIMPULSE_USER", "")
    password = env.get("DATAIMPULSE_PASS", "")
    port = env.get("DATAIMPULSE_PORT", "")
    if not (user and password and port):
        return None
    host = env.get("DATAIMPULSE_HOST") or DEFAULT_HOST
    cc = (country or proxy_country()).strip().lower()
    if "__cr." not in user:
        user = f"{user}__cr.{cc}"
    return user, password, host, port


def proxy_url(env: Optional[dict[str, str]] = None, country: Optional[str] = None) -> Optional[str]:
    """Direct `http://user:pass@host:port` URL for urllib-style clients."""
    creds = credentials(env, country)
    if not creds:
        return None
    user, password, host, port = creds
    return f"http://{user}:{password}@{host}:{port}"
