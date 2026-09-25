#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_watchdog_parity.py — scripts/watchdog_parity.py
"""Tests for the watchdog v2 shadow parity harness."""
from __future__ import annotations

import pytest
import watchdog_parity as wp

V2_LINE = (
    "Sep 24 23:58:43 host kv-fetch-env.py[1]: 2026-09-24T23:58:43Z [info ] "
    "[dry-run] svc:devforge-day-cycle failed: inactive (would service)"
)


def test_parse_dry_run_line_valid() -> None:
    d = wp.parse_dry_run_line(V2_LINE)
    assert d == {"component": "svc:devforge-day-cycle", "detail": "inactive", "would": "service"}


def test_parse_dry_run_line_ignores_non_dry_run() -> None:
    assert wp.parse_dry_run_line("normal log line without marker") is None


def test_parse_dry_run_line_component_with_colon() -> None:
    d = wp.parse_dry_run_line("[dry-run] timer:devforge-system-sync.timer failed: 900s idle (would timer_kick)")
    assert d is not None
    assert d["component"] == "timer:devforge-system-sync.timer"
    assert d["would"] == "timer_kick"


def _legacy(component: str, count: int = 1, reopens: int = 0) -> dict:
    return {"component": component, "count": str(count), "reopens": str(reopens)}


def test_build_report_matched_and_diff() -> None:
    lines = [
        V2_LINE,  # svc:devforge-day-cycle (also repeated)
        V2_LINE,
        "[dry-run] svc:only-v2 failed: down (would service)",  # v2-only
    ]
    rows = [_legacy("svc:devforge-day-cycle", count=2), _legacy("svc:only-legacy")]
    rep = wp.build_report(lines, rows)

    assert rep["v2_components"] == 2
    assert rep["legacy_components"] == 2
    assert rep["matched"] == ["svc:devforge-day-cycle"]
    assert rep["v2_only"] == ["svc:only-v2"]
    assert rep["legacy_only"] == ["svc:only-legacy"]

    by_comp = {c["component"]: c for c in rep["components"]}
    assert by_comp["svc:devforge-day-cycle"]["v2_count"] == 2
    assert by_comp["svc:devforge-day-cycle"]["verdict"] == "matched"
    assert by_comp["svc:only-v2"]["verdict"] == "v2_only"
    assert by_comp["svc:only-legacy"]["legacy_count"] == 1


def test_main_exit_1_on_parity_gap(monkeypatch) -> None:
    monkeypatch.setattr(wp, "_journal_lines", lambda *a, **k: [V2_LINE])
    monkeypatch.setattr(wp, "_legacy_rows", lambda *a, **k: [_legacy("svc:other")])
    assert wp.main(["--since", "2026-09-24T00:00:00Z", "--until", "2026-09-25T00:00:00Z"]) == 1


def test_main_exit_0_on_parity(monkeypatch) -> None:
    monkeypatch.setattr(wp, "_journal_lines", lambda *a, **k: [V2_LINE])
    monkeypatch.setattr(wp, "_legacy_rows", lambda *a, **k: [_legacy("svc:devforge-day-cycle")])
    assert wp.main(["--json"]) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
