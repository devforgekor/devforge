#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Verification: every actionable doc is registered in docs/INDEX.md.

Catches doc drift — a new plan/runbook/adr/spec that is never added to the
index (the index is the single map; see INDEX '성격 범례'). `reports/` is a
curated record set and intentionally excluded.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX_PATH = ROOT / "docs" / "INDEX.md"
CATEGORIES = ("plans", "runbooks", "adr", "specs")
SUFFIXES = (".md", ".yaml")


def test_all_actionable_docs_registered() -> None:
    index = INDEX_PATH.read_text(encoding="utf-8")
    missing: list[str] = []
    for category in CATEGORIES:
        for path in sorted((ROOT / "docs" / category).glob("*")):
            if path.is_file() and path.suffix in SUFFIXES and path.name not in index:
                missing.append(f"{category}/{path.name}")
    assert not missing, f"docs missing from INDEX.md: {missing}"
