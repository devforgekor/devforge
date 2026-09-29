#!/usr/bin/env python3.12
# Status: production
# Path: pytest 대상 — lib.prj.cli 입력 경로 자동 판정
"""에이전트가 플래그를 기억하지 않아도 되도록 크기만 보고 방식을 고른다.

실측 경계(2026-09-29, DeepSeek web): 55.8k자 통과 / 58.5k자 실패 → 상한 50,000.
"""

import pytest
from lib.prj.cli import MAX_CONTEXT_CHARS, EngineError, _resolve_source


class TestShouldChooseFileModeWhenBelowLimit:
    def test_should_use_file_for_small_document(self, tmp_path):
        f = tmp_path / "small.md"
        f.write_text("짧은 문서 " * 100, encoding="utf-8")
        path, nchars, mode = _resolve_source(str(f))
        assert mode == "file"
        assert nchars == len(f.read_text(encoding="utf-8"))
        assert path == str(f)


class TestShouldChooseChunkModeWhenAboveLimit:
    def test_should_switch_to_chunk_above_cap(self, tmp_path):
        f = tmp_path / "big.md"
        f.write_text("가" * (MAX_CONTEXT_CHARS + 1), encoding="utf-8")
        assert _resolve_source(str(f))[2] == "chunk"

    def test_should_keep_file_exactly_at_cap(self, tmp_path):
        f = tmp_path / "edge.md"
        f.write_text("가" * MAX_CONTEXT_CHARS, encoding="utf-8")
        assert _resolve_source(str(f))[2] == "file"


class TestShouldHandleArgvModeWhenNoFileGiven:
    def test_should_return_argv_when_no_source(self):
        assert _resolve_source(None) == (None, 0, "argv")


class TestShouldFailClosedWhenPathInvalid:
    def test_should_raise_when_file_missing(self):
        with pytest.raises(EngineError, match="file not found"):
            _resolve_source("/nonexistent/path.md")

    def test_should_raise_when_directory_has_no_markdown(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        with pytest.raises(EngineError, match="no .md files"):
            _resolve_source(str(d))


class TestShouldResolveDirectoryAsVaultWhenGiven:
    def test_should_pick_markdown_from_directory(self, tmp_path):
        d = tmp_path / "vault"
        d.mkdir()
        (d / "a.md").write_text("문서 A", encoding="utf-8")
        (d / "b.md").write_text("문서 B", encoding="utf-8")
        (d / "note.txt").write_text("무시", encoding="utf-8")
        path, _, mode = _resolve_source(str(d))
        assert path.endswith(".md")
        assert "note.txt" not in path
        assert mode == "file"

    def test_should_apply_chunk_mode_for_large_directory_member(self, tmp_path):
        d = tmp_path / "vault"
        d.mkdir()
        (d / "a.md").write_text("가" * (MAX_CONTEXT_CHARS + 1), encoding="utf-8")
        assert _resolve_source(str(d))[2] == "chunk"


class TestShouldExpandUserPathWhenTildeGiven:
    def test_should_expand_tilde(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "x.md").write_text("내용", encoding="utf-8")
        path, _, _ = _resolve_source("~/x.md")
        assert str(tmp_path) in path
