#!/usr/bin/env python3.11
# Status: production
# Path: imported by — lib/research/exa.py, lib/research/context7.py
"""Shared encrypted-key loader for research providers (exa, context7)."""

from __future__ import annotations

import os

SECRETS_PATH = os.path.expanduser("~/.config/devforge/secrets.env")


def ensure_env(env_name: str) -> None:
    """Load DEVFORGE_ENCRYPTION_PASSPHRASE and env_name from secrets.env if not already set."""
    if os.environ.get("DEVFORGE_ENCRYPTION_PASSPHRASE") and os.environ.get(env_name):
        return
    if not os.path.exists(SECRETS_PATH):
        return
    with open(SECRETS_PATH) as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("DEVFORGE_ENCRYPTION_PASSPHRASE="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ.setdefault("DEVFORGE_ENCRYPTION_PASSPHRASE", val)
            elif line.startswith(f"{env_name}="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                os.environ.setdefault(env_name, val)


def load_encrypted_keys(env_name: str) -> list[str]:
    """Return decrypted key pool from env_name (label:cipher,...). Empty if none."""
    ensure_env(env_name)
    from lib.auth.api_key_cipher import decrypt_data

    raw = os.environ.get(env_name, "")
    if not raw:
        return []
    keys: list[str] = []
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" in pair:
            _, cipher = pair.split(":", 1)
            cipher = cipher.strip()
            plain = decrypt_data(cipher)
            if plain is None:
                plain = cipher
            keys.append(plain)
    return keys
