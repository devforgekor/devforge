#!/usr/bin/env python3
# Status: production
# Path: tests/unit/test_baseline_daily.py
"""baseline-daily psql 실패 처리 테스트 — 실패 시 baseline 덮어쓰기가 중단되어야 한다."""

import importlib.util
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "baseline-daily.py"


def _load():
    spec = importlib.util.spec_from_file_location("baseline_daily", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestPsqlFailure:
    def test_should_raise_when_psql_returns_nonzero(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *a, **k: _fake_completed(stderr="could not connect to server", returncode=1),
        )
        with pytest.raises(mod.BaselineQueryError):
            mod.run("SELECT count(*) FROM turns")

    def test_should_raise_from_run_rows_when_psql_returns_nonzero(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(
            mod.subprocess,
            "run",
            lambda *a, **k: _fake_completed(returncode=2),
        )
        with pytest.raises(mod.BaselineQueryError):
            mod.run_rows("SELECT source, count(*) FROM turns GROUP BY source")

    def test_should_return_empty_dict_when_query_succeeds_with_no_rows(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _fake_completed(stdout=""))
        assert mod.run_rows("SELECT source, count(*) FROM turns GROUP BY source") == {}

    def test_should_return_stdout_when_psql_succeeds(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _fake_completed(stdout="9426\n"))
        assert mod.run("SELECT count(*) FROM turns") == "9426"

    def test_should_parse_pipe_separated_rows(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(
            mod.subprocess, "run", lambda *a, **k: _fake_completed(stdout="claude|399\nopencode|1842\n")
        )
        assert mod.run_rows("SELECT source, count(*) FROM turns GROUP BY source") == {
            "claude": "399",
            "opencode": "1842",
        }
