#!/usr/bin/env python3
# Status: production
# Path: main.py
"""JSON file persistence for cashbook data."""

from __future__ import annotations

import json
import re
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


def _normalize_date(d: str) -> str:
    """Convert YY.M.D. to YY.MM.DD. format."""
    m = re.match(r'^(\d{2})\.(\d{1,2})\.(\d{1,2})\.?$', d)
    if m:
        return f"{m.group(1)}.{m.group(2).zfill(2)}.{m.group(3).zfill(2)}."
    return d


def normalize_all_dates() -> int:
    """Normalize all dates in the data file. Returns count of changes."""
    cb = load()
    changed = 0
    for dep in cb.deposits:
        new_date = _normalize_date(dep.date)
        if new_date != dep.date:
            dep.date = new_date
            changed += 1
    for wit in cb.withdrawals:
        new_date = _normalize_date(wit.date)
        if new_date != wit.date:
            wit.date = new_date
            changed += 1
    if changed:
        save(cb)
    return changed
