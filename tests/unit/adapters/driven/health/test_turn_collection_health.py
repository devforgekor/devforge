#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/adapters/driven/health/
"""Tests for the turn collection health check."""

from __future__ import annotations

import json

import pytest

from devforge.adapters.driven.health.turn_collection_health import (
    COMPONENT,
    DEFAULT_CHECKPOINT,
    TurnCollectionHealthChecker,
)


def _write(tmp_path, entries):
    path = tmp_path / "collect_checkpoint.json"
    path.write_text(json.dumps({"opencode": entries}))
    return path


@pytest.mark.asyncio
async def test_should_report_healthy_when_no_session_is_parked(tmp_path) -> None:
    path = _write(tmp_path, {"a": {"count": 3, "mtime": 1.0}, "b": {"count": 1, "mtime": 1.0}})
    checks = await TurnCollectionHealthChecker(path).check_health()
    assert len(checks) == 1
    assert checks[0].component == "turn_collection"
    assert checks[0].is_healthy
    assert checks[0].metric_value == 1.0
    assert "quarantined=0/2" in checks[0].detail


@pytest.mark.asyncio
async def test_should_report_unhealthy_when_a_session_is_parked(tmp_path) -> None:
    path = _write(
        tmp_path,
        {
            "a": {"count": 3, "mtime": 1.0},
            "b": {
                "count": 1,
                "mtime": 1.0,
                "failures": 1,
                "fail_kind": "permanent",
                "quarantined_at": 99.0,
            },
        },
    )
    check = (await TurnCollectionHealthChecker(path).check_health())[0]
    assert not check.is_healthy
    assert check.metric_value == 0.5
    assert "quarantined=1/2" in check.detail
    assert "kinds=permanent" in check.detail


@pytest.mark.asyncio
async def test_should_report_healthy_when_checkpoint_is_missing(tmp_path) -> None:
    check = (await TurnCollectionHealthChecker(tmp_path / "absent.json").check_health())[0]
    assert check.is_healthy
    assert check.metric_value == 1.0


@pytest.mark.asyncio
async def test_should_report_healthy_when_checkpoint_is_corrupt(tmp_path) -> None:
    path = tmp_path / "collect_checkpoint.json"
    path.write_text("{not json")
    check = (await TurnCollectionHealthChecker(path).check_health())[0]
    assert check.is_healthy
    assert check.metric_value == 1.0


@pytest.mark.asyncio
async def test_should_ignore_entries_of_other_sources(tmp_path) -> None:
    path = tmp_path / "collect_checkpoint.json"
    path.write_text(json.dumps({"claude": {"a": {"count": 1, "mtime": 1.0, "quarantined_at": 9.0}}}))
    check = (await TurnCollectionHealthChecker(path).check_health())[0]
    assert check.is_healthy
    assert "quarantined=0/0" in check.detail


def test_should_default_to_live_checkpoint_path() -> None:
    assert DEFAULT_CHECKPOINT.exists()
    assert COMPONENT == "turn_collection"
