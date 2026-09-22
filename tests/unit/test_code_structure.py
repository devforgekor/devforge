#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Verification: every file listed in code-structure.yaml exists on disk.

code-structure.yaml is manually maintained (auto-generator retired); this test
catches drift when a file is added/moved/deleted without updating the YAML.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = ROOT / "docs" / "architecture" / "code-structure.yaml"


def _listed_files() -> list[str]:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    return [
        f"{mod['path']}{fname}"
        for mod in data.get("modules", [])
        for fname in (mod.get("files") or {})
    ]


def test_code_structure_lists_existing_files() -> None:
    missing = [p for p in _listed_files() if not (ROOT / p).exists()]
    assert not missing, f"stale entries in code-structure.yaml: {missing}"


def test_code_structure_has_no_duplicate_paths() -> None:
    paths = [m["path"] for m in yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))["modules"]]
    dupes = sorted({p for p in paths if paths.count(p) > 1})
    assert not dupes, f"duplicate module paths: {dupes}"
