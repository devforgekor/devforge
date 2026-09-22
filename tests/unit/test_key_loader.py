#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for the shared provider key loader (round-robin ready)."""
from __future__ import annotations

from lib.auth.key_loader import load_api_keys


def test_per_account_keys_auto_collected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_MESIDS_API_KEY", "AIza-1")
    monkeypatch.setenv("GEMINI_MINIPARK4U_API_KEY", "AIza-2")
    keys = load_api_keys("GEMINI")
    assert ("mesids", "AIza-1") in keys
    assert ("minipark4u", "AIza-2") in keys


def test_service_prefix_for_rotator(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("BRAVE_API_KEYS", raising=False)
    monkeypatch.setenv("BRAVE_MESIDS_API_KEY", "b1")
    keys = load_api_keys("BRAVE", service="brave")
    assert keys[0][0] == "brave:mesids"


def test_consolidated_takes_priority(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.setenv("GEMINI_MESIDS_API_KEY", "AIza-1")
    keys = load_api_keys("GEMINI")
    assert len(keys) == 2


def test_single_key_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "solo")
    keys = load_api_keys("GEMINI")
    assert keys == [("default", "solo")]


def test_no_keys_returns_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for k in list(__import__("os").environ):
        if k.startswith("GEMINI_"):
            monkeypatch.delenv(k, raising=False)
    assert load_api_keys("GEMINI") == []
