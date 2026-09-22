#!/usr/bin/env python3
# Status: production
# Path: imported by — proxies/gemini_openai.py, lib/research/web.py
"""Unified provider API-key loader — round-robin ready, env-driven.

All rotating providers (BRAVE, CONTEXT7, EXA, GEMINI, TAVILY, YOUCOM) share
this one loader. Keys are supplied entirely via environment variables (Azure KV
→ env via kv-fetch-env.py); changing the env vars changes the keys, so callers
need no provider-specific code.

Resolution order for `{PREFIX}`:
  1. ``{PREFIX}_API_KEYS``   — consolidated; entries are ``key`` or ``name:cipher``
  2. ``{PREFIX}_{ACCOUNT}_API_KEY`` — per-account plaintext, auto-collected (sorted)
  3. ``{PREFIX}_API_KEY``    — single key

Returns ``[(name, plaintext_key), ...]`` suitable for ``KeyRotator``.
"""
import os
import sys

STATE_FILE = os.path.expanduser("~/.cache/devforge/gemini_rotator_state.json")

_ACCOUNT_SUFFIX = "_API_KEY"


def load_api_keys(provider_prefix: str = "GEMINI", service: str | None = None) -> list:
    """Load provider keys from env. Returns [(name, plaintext_key), ...].

    provider_prefix: env prefix, e.g. "GEMINI", "BRAVE", "EXA", "TAVILY",
                     "YOUCOM", "CONTEXT7".
    service: optional label; when set, names are prefixed ``"{service}:{account}"``
             (the KeyRotator convention used by lib/research/web.py).
    """
    prefix = provider_prefix.upper()

    def _name(n: str) -> str:
        return f"{service}:{n}" if service else n

    # 1. Consolidated list
    keys_str = os.getenv(f"{prefix}_API_KEYS", "")
    if keys_str:
        from lib.auth.api_key_cipher import decrypt_data

        keys = []
        for item in keys_str.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" in item:
                name, cipher = item.split(":", 1)
                plain = decrypt_data(cipher.strip())
                if plain is None:
                    print(f"경고: 키 복호화 실패 — {name.strip()} (평문으로 시도)", file=sys.stderr)
                    plain = cipher.strip()
                keys.append((_name(name.strip()), plain))
            else:
                plain = decrypt_data(item)
                keys.append((_name(f"key-{len(keys)}"),
                             plain if plain is not None else item))
        return keys

    # 2. [WHY] Per-account plaintext keys (Azure KV migration, 2026-09):
    # {PREFIX}_{ACCOUNT}_API_KEY. Auto-collect so the rotator can round-robin.
    named = []
    for env_key, env_val in sorted(os.environ.items()):
        if env_key.startswith(f"{prefix}_") and env_key.endswith(_ACCOUNT_SUFFIX):
            account = env_key[len(prefix) + 1 : -len(_ACCOUNT_SUFFIX)]
            if account and env_val.strip():
                named.append((_name(account.lower()),
                              env_val.strip().strip('"').strip("'")))
    if named:
        return named

    # 3. Single key
    single = os.getenv(f"{prefix}_API_KEY", "")
    if single:
        return [(_name("default"), single)]

    return []
