#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Behavior: handover_db completed_log write + YAML regenerate.

Pins: insert-only (no overwrite), clean_log_text unwrap, NULL checkpoint_id,
regenerate pulls recent 50 distinct log_text into handover.yaml.
"""
from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.characterization

handover_db = pytest.importorskip("lib.handover_db")


def test_clean_log_text_unwraps_session_dict() -> None:
    raw = "{'session 2026-09-23': 'fixed watchdog kick'}"
    assert handover_db.clean_log_text(raw) == "fixed watchdog kick"


def test_clean_log_text_passthrough_plain() -> None:
    assert handover_db.clean_log_text("plain entry") == "plain entry"


def test_cp_sql_none_renders_null() -> None:
    assert handover_db._cp_sql(None) == "NULL"
    assert handover_db._cp_sql(42) == "42"


def test_write_completed_log_inserts_when_absent(monkeypatch: Any) -> None:
    inserted: list[str] = []
    monkeypatch.setattr(handover_db, "_pj", lambda _sql: [])
    monkeypatch.setattr(handover_db, "_psql", lambda sql: inserted.append(sql) or "")
    handover_db._write_completed_log(["cp entry A"], 7)
    assert len(inserted) == 1
    assert "INSERT INTO completed_log" in inserted[0]
    assert "cp entry A" in inserted[0]
    assert "VALUES (7," in inserted[0]


def test_write_completed_log_skips_duplicate(monkeypatch: Any) -> None:
    inserted: list[str] = []
    monkeypatch.setattr(handover_db, "_pj", lambda _sql: [{"?column?": 1}])
    monkeypatch.setattr(handover_db, "_psql", lambda sql: inserted.append(sql) or "")
    handover_db._write_completed_log(["dup entry"], None)
    assert inserted == []


def test_write_completed_log_none_cp_renders_null(monkeypatch: Any) -> None:
    inserted: list[str] = []
    monkeypatch.setattr(handover_db, "_pj", lambda _sql: [])
    monkeypatch.setattr(handover_db, "_psql", lambda sql: inserted.append(sql) or "")
    handover_db._write_completed_log(["orphan"], None)
    assert "VALUES (NULL," in inserted[0]


def test_regenerate_handover_yaml_writes_sections(tmp_path: Any, monkeypatch: Any) -> None:
    out = tmp_path / "handover.yaml"
    monkeypatch.setattr(handover_db, "HANDOVER_FILE", out)

    def fake_pj(sql: str) -> list[dict[str, Any]]:
        if "session_checkpoints" in sql:
            return [{
                "id": 9,
                "created_at": "2026-09-23 00:00:00",
                "summary": "s",
                "total_files": 1,
                "recent_files": {},
                "git_state": {},
                "task": None,
            }]
        if "FROM decisions" in sql:
            return [{"decision_id": "d1", "detail": "dec", "status": "open", "decision_text": None}]
        if "FROM known_issues" in sql:
            return [{"issue_id": None, "issue_text": "open issue", "detail": None, "resolved": False}]
        if "completed_log" in sql:
            return [{"log_text": "did the thing"}]
        return []

    monkeypatch.setattr(handover_db, "_pj", fake_pj)
    handover_db.regenerate_handover_yaml()
    text = out.read_text()
    assert "did the thing" in text
    assert "open issue" in text
    assert "last_checkpoint" in text


def test_regenerate_handover_yaml_noop_without_checkpoint(monkeypatch: Any, tmp_path: Any) -> None:
    out = tmp_path / "handover.yaml"
    monkeypatch.setattr(handover_db, "HANDOVER_FILE", out)
    monkeypatch.setattr(handover_db, "_pj", lambda _sql: [])
    handover_db.regenerate_handover_yaml()
    assert not out.exists()


def test_db_write_checkpoint_skip_uses_null_cp(monkeypatch: Any) -> None:
    calls: dict[str, Any] = {}
    monkeypatch.setattr(handover_db, "_insert_checkpoint", lambda cp: calls.setdefault("cp", cp) or 1)
    monkeypatch.setattr(handover_db, "_write_decisions", lambda d, c: calls.setdefault("dec", (d, c)))
    monkeypatch.setattr(handover_db, "_write_known_issues", lambda i, c: calls.setdefault("iss", (i, c)))
    monkeypatch.setattr(handover_db, "_write_completed_log", lambda logs, c: calls.setdefault("log", (logs, c)))
    monkeypatch.setattr(handover_db, "regenerate_handover_yaml", lambda: calls.setdefault("regen", True))
    handover_db.db_write_checkpoint(
        {"summary": "x"},
        {"completed_log": ["a"]},
        skip_checkpoint=True,
    )
    assert "cp" not in calls
    assert calls["log"] == (["a"], None)
    assert calls.get("regen") is True
