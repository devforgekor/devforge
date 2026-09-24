#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Doc link drift check (docs-as-code / docdrift pattern).

Two directions:
  1. no broken references — every `plans/..md`/`reports/..md`/... reference in
     docs/**/*.md must exist under docs/.
  2. expected cross-links — the design docs must point at each other and the
     refactoring plan must point at the operational-architecture docs.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
# repo-relative doc roots that must resolve under docs/
DOC_ROOTS = (
    "plans/", "reports/", "runbooks/", "adr/", "specs/",
    "architecture/", "operations/", "security/", "refactoring/",
)
REF_RE = re.compile(r"`((?:" + "|".join(re.escape(r) for r in DOC_ROOTS) + r")[A-Za-z0-9_./-]+\.(?:md|yaml))`")


def _all_docs() -> list[Path]:
    return [p for p in DOCS.rglob("*.md") if "_archive" not in p.parts and "archive" not in p.parts]


# proposed-but-not-yet-created files legitimately referenced by design docs
PLANNED = {
    "specs/mcp-inventory.yaml",
    "specs/heartbeat-registry.yaml",
}


def test_no_broken_doc_references() -> None:
    missing: list[str] = []
    for doc in _all_docs():
        for ref in REF_RE.findall(doc.read_text(encoding="utf-8")):
            if ref in PLANNED:
                continue
            if not (DOCS / ref).exists():
                missing.append(f"{doc.relative_to(ROOT)} -> {ref}")
    assert not missing, f"broken doc references: {missing}"


# (source, must-reference) — the intended cross-links / pointers
EXPECTED = [
    ("plans/system-reference-architecture.md", "plans/detection-remediation-architecture.md"),
    ("plans/system-reference-architecture.md", "plans/fitness-functions-heartbeat-drift-guide.md"),
    ("plans/system-reference-architecture.md", "reports/incident-issue-pr-loop-audit-20260923.md"),
    ("plans/detection-remediation-architecture.md", "reports/incident-issue-pr-loop-audit-20260923.md"),
    ("plans/fitness-functions-heartbeat-drift-guide.md", "reports/systemic-wiring-gap-analysis-20260924.md"),
    ("REFACTORING_PLAN.md", "plans/system-reference-architecture.md"),  # pointer (P1)
    ("ARCHITECTURE.md", "plans/system-reference-architecture.md"),  # pointer (P3)
]


def test_expected_cross_links() -> None:
    missing: list[str] = []
    for src, target in EXPECTED:
        text = (DOCS / src).read_text(encoding="utf-8")
        if target not in text and Path(target).name not in text:
            missing.append(f"{src} does not reference {target}")
    assert not missing, f"missing cross-links: {missing}"
