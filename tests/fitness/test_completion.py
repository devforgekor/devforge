#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/fitness/
"""Fitness: alembic migrations form one complete, unambiguous chain.

Catches migration drift that `alembic upgrade head` cannot survive: a forked
history (two heads) or a `down_revision` pointing at a revision that does not
exist. CI-portable — parses the version files, no DB connection required.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "alembic" / "versions"
REV_RE = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)
DOWN_RE = re.compile(r"^down_revision(?::[^=\n]+)?\s*=\s*[\"']([^\"']+)[\"']", re.MULTILINE)


def _revisions() -> list[tuple[str, str | None, Path]]:
    out: list[tuple[str, str | None, Path]] = []
    for path in sorted(VERSIONS.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        m = REV_RE.search(text)
        if not m:
            continue
        d = DOWN_RE.search(text)
        out.append((m.group(1), d.group(1) if d else None, path))
    return out


def test_alembic_has_exactly_one_head() -> None:
    revs = _revisions()
    assert revs, "no alembic revisions found"
    revisions = [r for r, _, _ in revs]
    assert len(revisions) == len(set(revisions)), f"duplicate revision ids: {revisions}"
    parents = {d for _, d, _ in revs if d}
    heads = [r for r in revisions if r not in parents]
    assert len(heads) == 1, f"expected exactly 1 head, got {heads}"


def test_alembic_parents_exist() -> None:
    revs = _revisions()
    revisions = {r for r, _, _ in revs}
    missing = [(p.name, d) for _, d, p in revs if d and d not in revisions]
    assert not missing, f"down_revision points at missing revisions: {missing}"


def test_error_record_migration_adds_structured_columns() -> None:
    # [WHY] error-record design §1.5: additive context_jsonb + action_error + GIN.
    text = (VERSIONS / "20260923_error_record.py").read_text(encoding="utf-8")
    assert "context_jsonb" in text, "error_record migration missing context_jsonb"
    assert "action_error" in text, "error_record migration missing action_error"
