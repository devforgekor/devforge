#!/usr/bin/env python3
# Status: production
# Path: main.py
"""JSON file persistence for cashbook data."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from models import CashBook

DATA_DIR = Path(__file__).parent / "data"
DATA_FILE = DATA_DIR / "cashbook.json"
_lock = threading.Lock()


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def load() -> CashBook:
    _ensure_dir()
    if not DATA_FILE.exists():
        return CashBook()
    with _lock:
        raw = DATA_FILE.read_text(encoding="utf-8")
        return CashBook.model_validate_json(raw)


def save(cb: CashBook) -> None:
    _ensure_dir()
    with _lock:
        DATA_FILE.write_text(
            cb.model_dump_json(indent=2),
            encoding="utf-8",
        )
