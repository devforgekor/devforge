#!/usr/bin/env python3
# Status: production
"""Individual lint rule check functions."""

import ast
import re
from pathlib import Path
from typing import Dict, List, Tuple

from lint_rules.data import (
    MODEL_NAME_OK_FILES, MODEL_SIZE_PATTERN, MODEL_BRAND_PATTERN,
    ABBREVIATION_OK_FILES, BANNED_NAMES, _KOREAN_RE,
)


def check_status_header(filepath: Path, relpath_root: Path) -> List[Dict]:
    if filepath.name == "__init__.py" and filepath.stat().st_size < 100:
        return []
    try:
        content = filepath.read_text()
    except Exception:
        return []
    violations = []
    if not re.search(r"^# Status:", content, re.MULTILINE):
        violations.append({
            "rule": "status-header",
            "severity": "P0",
            "message": "Missing '# Status:' header",
            "file": str(filepath.relative_to(relpath_root)),
        })
    return violations


def check_utcnow(filepath: Path, relpath_root: Path) -> List[Dict]:
    if filepath.name == "lint_rules.py":
        return []
    try:
        content = filepath.read_text()
    except Exception:
        return []
    violations = []
    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        if "utcnow()" in line and not line.strip().startswith("#"):
            violations.append({
                "rule": "no-utcnow",
                "severity": "P0",
                "message": "Use datetime.now(timezone.utc) instead of utcnow()",
                "file": str(filepath.relative_to(relpath_root)),
                "line": i,
            })
    return violations


def check_bare_except(filepath: Path, relpath_root: Path) -> List[Dict]:
    try:
        content = filepath.read_text()
    except Exception:
        return []
    violations = []
    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if stripped == "except:" or stripped.startswith("except:"):
            if "except:" == stripped or not stripped.startswith("except: "):
                violations.append({
                    "rule": "no-bare-except",
                    "severity": "P0",
                    "message": "Use 'except Exception:' instead of bare 'except:'",
                    "file": str(filepath.relative_to(relpath_root)),
                    "line": i,
                })
    return violations


def _check_name_parts(name: str, filepath: Path, lineno: int, relpath: str) -> List[Dict]:
    violations = []
    parts = name.split("_")
    for part in parts:
        if MODEL_SIZE_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-identifier",
                "severity": "P1",
                "message": f"'{name}' contains model size '{part}'. Use role name instead.",
                "file": relpath,
                "line": lineno,
            })
        if MODEL_BRAND_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-identifier",
                "severity": "P1",
                "message": f"'{name}' contains model brand '{part}'. Use role name instead.",
                "file": relpath,
                "line": lineno,
            })
    return violations


def _extract_all_identifiers(content: str) -> List[Tuple[str, int]]:
    identifiers = set()
    try:
        tree = ast.parse(content)
    except SyntaxError:
        tree = None

    if tree:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                identifiers.add((node.name, node.lineno))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        identifiers.add((target.id, target.lineno))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                identifiers.add((node.target.id, node.lineno))

    for i, line in enumerate(content.split("\n"), 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
            continue
        for match in re.finditer(r"\b([a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)*)\b", line):
            name = match.group(1)
            has_model = False
            for part in name.split("_"):
                if MODEL_SIZE_PATTERN.fullmatch(part) or MODEL_BRAND_PATTERN.fullmatch(part):
                    has_model = True
                    break
            if has_model:
                identifiers.add((name, i))

    return sorted(identifiers, key=lambda x: x[1])


def check_identifier_naming(filepath: Path, relpath_root: Path) -> List[Dict]:
    relpath = str(filepath.relative_to(relpath_root))
    violations = []

    skip_model_check = relpath in MODEL_NAME_OK_FILES

    stem = filepath.stem.lower()
    for part in stem.split("_"):
        if MODEL_SIZE_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-filename",
                "severity": "P1",
                "message": f"Filename contains model size '{part}': '{filepath.name}'",
                "file": relpath,
            })
        if MODEL_BRAND_PATTERN.fullmatch(part):
            violations.append({
                "rule": "no-model-in-filename",
                "severity": "P1",
                "message": f"Filename contains model brand '{part}': '{filepath.name}'",
                "file": relpath,
            })

    try:
        content = filepath.read_text()
    except Exception:
        return violations

    identifiers = _extract_all_identifiers(content)
    skip_abbreviation_check = relpath in ABBREVIATION_OK_FILES

    for name, lineno in identifiers:
        if not skip_model_check:
            violations.extend(_check_name_parts(name, filepath, lineno, relpath))
        if not skip_abbreviation_check and name in BANNED_NAMES:
            violations.append({
                "rule": "no-abbreviation",
                "severity": "P2",
                "message": f"'{name}' is a banned abbreviation. Rename to '{BANNED_NAMES[name]}'.",
                "file": relpath,
                "line": lineno,
            })

    return violations


def check_doc_language(filepath: Path, relpath_root: Path) -> List[Dict]:
    violations = []
    if filepath.suffix.lower() not in (".yaml", ".yml"):
        return violations
    if "_archive" in filepath.parts:
        return violations
    try:
        content = filepath.read_text()
    except Exception:
        return violations

    lines = content.split("\n")
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        korean_chars = _KOREAN_RE.findall(line)
        if korean_chars:
            relpath = str(filepath.relative_to(relpath_root))
            snippet = line.strip()[:80]
            violations.append({
                "rule": "english-only-docs",
                "severity": "P1",
                "message": f"Korean chars in machine-readable doc: '{snippet}' ({len(korean_chars)} chars)",
                "file": relpath,
                "line": i,
            })
    return violations
