#!/usr/bin/env python3
# Status: production
# Path: tests/unit/test_dev_pipeline.py — scripts/lib/dev_pipeline.py (create_pr 가드, Aging WIP)
"""create_pr empty-branch guard and Aging WIP (Kanban Work Item Age) detection."""

import datetime
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from lib import dev_pipeline as dp  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


class _FakeResult:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_gh(ahead_by: int = 2, compare_rc: int = 0, pr_url: str = "https://example/pr/1"):
    calls: List[List[str]] = []

    def fake(args: List[str], timeout: int = 30) -> _FakeResult:
        calls.append(args)
        if args[:1] == ["api"]:
            return _FakeResult(compare_rc, str(ahead_by))
        if args[:2] == ["pr", "create"]:
            return _FakeResult(0, pr_url)
        return _FakeResult(0, "")

    return fake, calls


def _patch_state(monkeypatch, state: dict) -> None:
    monkeypatch.setattr(dp, "_load_state", lambda: state)
    monkeypatch.setattr(dp, "_save_state", lambda data: None)


def _empty_state() -> dict:
    return {"seen_issues": [], "claimed": {}, "pr_created": {}}


def test_should_skip_empty_pr_when_branch_not_ahead(monkeypatch):
    fake, calls = _fake_gh(ahead_by=0)
    monkeypatch.setattr(dp, "_run_gh", fake)
    _patch_state(monkeypatch, _empty_state())

    assert dp.create_pr(8) is None
    assert not any(c[:2] == ["pr", "create"] for c in calls)


def test_should_skip_when_branch_missing(monkeypatch):
    fake, calls = _fake_gh(compare_rc=1)
    monkeypatch.setattr(dp, "_run_gh", fake)
    _patch_state(monkeypatch, _empty_state())

    assert dp.create_pr(8) is None
    assert not any(c[:2] == ["pr", "create"] for c in calls)


def test_should_create_pr_when_branch_ahead(monkeypatch):
    fake, calls = _fake_gh(ahead_by=2, pr_url="https://example/pr/8")
    monkeypatch.setattr(dp, "_run_gh", fake)
    _patch_state(monkeypatch, _empty_state())

    assert dp.create_pr(8) == "https://example/pr/8"
    assert any(c[:2] == ["pr", "create"] for c in calls)


def _claimed_state(
    claimed_at_8: str, claimed_at_9: str, pr_created: Optional[dict] = None
) -> dict:
    return {
        "seen_issues": [8, 9],
        "claimed": {
            "8": {"title": "netdata", "claimed_at": claimed_at_8},
            "9": {"title": "ebook", "claimed_at": claimed_at_9},
        },
        "pr_created": pr_created or {},
    }


def test_should_flag_aging_wip_when_past_sle(monkeypatch):
    state = _claimed_state("2026-09-14T00:00:00Z", "2026-09-23T00:00:00Z")
    monkeypatch.setattr(dp, "_load_state", lambda: state)

    aged = dp.aged_work_items(sle_days=3, now=NOW)
    assert [a["issue"] for a in aged] == [8]
    assert aged[0]["age_days"] > 3


def test_should_exclude_aging_wip_when_pr_created(monkeypatch):
    state = _claimed_state(
        "2026-09-14T00:00:00Z", "2026-09-23T00:00:00Z", pr_created={"8": {"url": "x"}}
    )
    monkeypatch.setattr(dp, "_load_state", lambda: state)

    assert dp.aged_work_items(sle_days=3, now=NOW) == []


def test_should_ignore_unparseable_claimed_at(monkeypatch):
    state = _claimed_state("not-a-date", "2026-09-01T00:00:00Z")
    monkeypatch.setattr(dp, "_load_state", lambda: state)

    aged = dp.aged_work_items(sle_days=3, now=NOW)
    assert [a["issue"] for a in aged] == [9]
