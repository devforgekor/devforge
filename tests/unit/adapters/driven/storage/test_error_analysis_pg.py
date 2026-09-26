#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/storage/
"""Tests for error-record analysis Postgres adapter helpers."""
from __future__ import annotations

from datetime import timezone

from devforge.adapters.driven.storage.error_analysis_pg import as_datetime


def test_should_parse_iso_with_offset() -> None:
    dt = as_datetime("2026-09-01T00:00:00+00:00")
    assert dt.tzinfo is not None


def test_should_parse_z_suffix_and_assume_utc_for_naive() -> None:
    assert as_datetime("2026-09-01T00:00:00Z").utcoffset() is not None
    assert as_datetime("2026-09-01T00:00:00").tzinfo is timezone.utc
