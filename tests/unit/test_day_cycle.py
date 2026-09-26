#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/
"""Unit tests for run_full_cycle — owned-stage ordering, failure continuation, budget."""

from __future__ import annotations

import pytest

from devforge.application.day_cycle import Fts5RefreshStage, run_full_cycle
from devforge.application.orchestrator import PipelineBudgetError


class RecordingEmbedStage:
    name = "embed"

    def __init__(self, record: list[str], fail: bool = False) -> None:
        self._record = record
        self._fail = fail

    def run(self) -> str:
        self._record.append("embed")
        if self._fail:
            raise RuntimeError("embed down")
        return "embed: embedded=1"


def test_should_run_fts5_then_embed_when_both_stages_provided() -> None:
    record: list[str] = []

    def fts5() -> str:
        record.append("fts5")
        return "fts5_refresh OK"

    results = run_full_cycle(
        embed_stage=RecordingEmbedStage(record),
        fts5_refresh=fts5,
        budget_sec=60,
    )
    assert record == ["fts5", "embed"]
    assert results == ["fts5_refresh OK", "embed: embedded=1"]


def test_should_run_embed_only_when_fts5_omitted() -> None:
    record: list[str] = []
    results = run_full_cycle(embed_stage=RecordingEmbedStage(record), budget_sec=60)
    assert record == ["embed"]
    assert results == ["embed: embedded=1"]


def test_should_continue_to_embed_when_fts5_refresh_raises() -> None:
    record: list[str] = []

    def broken_fts5() -> str:
        raise RuntimeError("fts5 boom")

    results = run_full_cycle(
        embed_stage=RecordingEmbedStage(record),
        fts5_refresh=broken_fts5,
        budget_sec=60,
    )
    assert record == ["embed"]
    assert "FAILED" in results[0]
    assert results[1] == "embed: embedded=1"


def test_should_raise_budget_error_when_budget_already_expired() -> None:
    record: list[str] = []
    with pytest.raises(PipelineBudgetError):
        run_full_cycle(embed_stage=RecordingEmbedStage(record), budget_sec=0)
    assert record == []


def test_should_propagate_embed_failure_when_stage_raises() -> None:
    record: list[str] = []
    with pytest.raises(RuntimeError, match="embed down"):
        run_full_cycle(embed_stage=RecordingEmbedStage(record, fail=True), budget_sec=60)
    assert record == ["embed"]


def test_should_return_failed_status_when_fts5_stage_handles_exception() -> None:
    stage = Fts5RefreshStage(lambda: (_ for _ in ()).throw(ValueError("x")))
    assert "FAILED" in stage.run()
