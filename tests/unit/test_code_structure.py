#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Bidirectional verification of code-structure.yaml against the source tree.

code-structure.yaml is manually maintained (auto-generator retired); these tests
catch drift in BOTH directions:
  doc -> code: every listed file exists on disk
  code -> doc: every source .py under scripts/ and src/devforge is listed
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML_PATH = ROOT / "docs" / "architecture" / "code-structure.yaml"
SCAN_ROOTS = ("scripts", "src/devforge")


def _listed_files() -> list[str]:
    data = yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))
    return [
        f"{mod['path']}{fname}"
        for mod in data.get("modules", [])
        for fname in (mod.get("files") or {})
    ]


def _actual_py_files() -> set[str]:
    actual: set[str] = set()
    for scan_root in SCAN_ROOTS:
        for dirpath, _dirs, filenames in os.walk(ROOT / scan_root):
            if "_archive" in dirpath or "__pycache__" in dirpath:
                continue
            for fname in filenames:
                if fname.endswith(".py"):
                    actual.add(str((Path(dirpath) / fname).relative_to(ROOT)))
    return actual


def test_code_structure_lists_existing_files() -> None:
    missing = [p for p in _listed_files() if not (ROOT / p).exists()]
    assert not missing, f"stale entries in code-structure.yaml: {missing}"


def test_code_structure_covers_all_source_files() -> None:
    unlisted = sorted(_actual_py_files() - set(_listed_files()))
    assert not unlisted, f"source files missing from code-structure.yaml: {unlisted}"


def test_code_structure_has_no_duplicate_paths() -> None:
    paths = [m["path"] for m in yaml.safe_load(YAML_PATH.read_text(encoding="utf-8"))["modules"]]
    dupes = sorted({p for p in paths if paths.count(p) > 1})
    assert not dupes, f"duplicate module paths: {dupes}"
