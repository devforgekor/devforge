#!/usr/bin/env python3
# Status: production
# Path: all pipeline files, lib modules
"""Shared utilities — log, timestamp, and other common helpers."""

from datetime import datetime, timezone


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{timestamp()}] {msg}", flush=True)

import re as _re
_THINK_RE = _re.compile(r"<think[^>]*>.*?</think>", _re.DOTALL)


def strip_think(text: str) -> str:
    """Remove <think>...</think> blocks from LLM output."""
    return _THINK_RE.sub("", text).strip()
