"""Tests for src/common_lib/core/file_io.py"""
import json
import os
from pathlib import Path

import pytest

from common_lib.core.file_io import (
    append_jsonl,
    atomic_write_jsonl,
    read_json,
    read_jsonl,
    write_json,
)


class TestReadJsonl:
    def test_missing_file_returns_empty(self, tmp_path):
        result = read_jsonl(tmp_path / "nope.jsonl")
        assert result == []

    def test_reads_rows(self, tmp_path):
        p = tmp_path / "data.jsonl"
        p.write_text('{"a":1}\n{"b":2}\n', encoding="utf-8")
        result = read_jsonl(p)
        assert result == [{"a": 1}, {"b": 2}]

    def test_ignores_blank_lines(self, tmp_path):
        p = tmp_path / "data.jsonl"
        p.write_text('{"a":1}\n\n{"b":2}\n', encoding="utf-8")
        assert len(read_jsonl(p)) == 2

    def test_unicode_content(self, tmp_path):
        p = tmp_path / "data.jsonl"
        p.write_text('{"name":"한글"}\n', encoding="utf-8")
        assert read_jsonl(p)[0]["name"] == "한글"


class TestAppendJsonl:
    def test_creates_file(self, tmp_path):
        p = tmp_path / "sub" / "data.jsonl"
        append_jsonl(p, [{"x": 1}])
        assert p.exists()
        assert read_jsonl(p) == [{"x": 1}]

    def test_appends_multiple_times(self, tmp_path):
        p = tmp_path / "data.jsonl"
        append_jsonl(p, [{"a": 1}])
        append_jsonl(p, [{"b": 2}])
        assert read_jsonl(p) == [{"a": 1}, {"b": 2}]

    def test_empty_rows(self, tmp_path):
        p = tmp_path / "data.jsonl"
        append_jsonl(p, [])
        # File should not be created or be empty
        assert not p.exists() or p.read_text() == ""


class TestAtomicWriteJsonl:
    def test_creates_and_reads(self, tmp_path):
        p = tmp_path / "data.jsonl"
        atomic_write_jsonl(p, [{"a": 1}, {"b": 2}])
        assert read_jsonl(p) == [{"a": 1}, {"b": 2}]

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "data.jsonl"
        atomic_write_jsonl(p, [{"old": True}])
        atomic_write_jsonl(p, [{"new": True}])
        assert read_jsonl(p) == [{"new": True}]

    def test_no_temp_file_remains(self, tmp_path):
        p = tmp_path / "data.jsonl"
        atomic_write_jsonl(p, [{"a": 1}])
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        assert files[0] == p


class TestReadJson:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        assert read_json(tmp_path / "nope.json") == {}

    def test_missing_file_returns_default(self, tmp_path):
        assert read_json(tmp_path / "nope.json", default={"key": "val"}) == {"key": "val"}

    def test_reads_content(self, tmp_path):
        p = tmp_path / "data.json"
        p.write_text('{"hello": "world"}', encoding="utf-8")
        assert read_json(p) == {"hello": "world"}

    def test_invalid_json_returns_default(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert read_json(p) == {}

    def test_invalid_json_with_default(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        assert read_json(p, default={"fallback": True}) == {"fallback": True}


class TestWriteJson:
    def test_creates_file(self, tmp_path):
        p = tmp_path / "out.json"
        write_json(p, {"x": 1})
        assert p.exists()

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "dir" / "out.json"
        write_json(p, {"x": 1})
        assert p.exists()

    def test_readable(self, tmp_path):
        p = tmp_path / "out.json"
        write_json(p, {"name": "테스트"})
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["name"] == "테스트"

    def test_roundtrip(self, tmp_path):
        p = tmp_path / "out.json"
        original = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
        write_json(p, original)
        assert read_json(p) == original
