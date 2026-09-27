#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/
"""Unit tests for shadow_diff_logger parse_metrics — JSONL gate evidence format."""

from __future__ import annotations

import shadow_diff_logger

SAMPLE = """=== shadow_diff (embed) ===
model          : qwen3-embedding-8b-v1
tolerance      : 0.001
since window   : 2026-09-20T00:00:00
prod chunks    : 1281
shadow chunks  : 1244
matched        : 1244
missing(real)  : 14
missing(legacy_raw, intentional) : 23
missing(sentinel, intentional)   : 0
extra          : 0
text mismatch  : 583 (excluded: 583)
vector mismatch: 0

RESULT: diff=14 ❌ (intentional excluded: 23)
"""

PASSED_SAMPLE = """prod chunks    : 1281
shadow chunks  : 1281
matched        : 1281
missing(real)  : 0
text mismatch  : 583 (excluded: 583)
vector mismatch: 0

RESULT: diff=0 ✅ (intentional excluded: 23)
"""


def test_should_parse_leading_number_when_note_follows_counter() -> None:
    data = shadow_diff_logger.parse_metrics(SAMPLE)
    assert data["text_mismatch"] == 583
    assert data["prod_chunks"] == 1281
    assert data["shadow_chunks"] == 1244
    assert data["matched"] == 1244
    assert data["missing_real"] == 14
    assert data["vector_mismatch"] == 0


def test_should_mark_failed_when_diff_nonzero() -> None:
    data = shadow_diff_logger.parse_metrics(SAMPLE)
    assert data["diff"] == 14
    assert data["passed"] is False


def test_should_mark_passed_when_diff_zero() -> None:
    data = shadow_diff_logger.parse_metrics(PASSED_SAMPLE)
    assert data["diff"] == 0
    assert data["passed"] is True


def test_should_ignore_intentional_missing_classes_when_parsing() -> None:
    data = shadow_diff_logger.parse_metrics(SAMPLE)
    assert "legacy_raw" not in str(data.get("missing_real"))
    assert data["missing_real"] == 14
