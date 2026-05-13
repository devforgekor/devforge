"""Tests for src/common_lib/core/text.py"""
import pytest
from common_lib.core.text import (
    clean_text,
    count_tokens,
    normalize,
    normalize_for_id,
    strip_html,
)


class TestCleanText:
    def test_collapses_whitespace(self):
        assert clean_text("hello   world") == "hello world"

    def test_strips_leading_trailing(self):
        assert clean_text("  hi  ") == "hi"

    def test_nfc_normalization(self):
        # NFC: composed form
        nfc = "\u00e9"  # é (precomposed)
        nfd = "e\u0301"  # é (decomposed)
        assert clean_text(nfd) == nfc

    def test_non_string_returns_empty(self):
        assert clean_text(None) == ""  # type: ignore[arg-type]
        assert clean_text(123) == ""  # type: ignore[arg-type]

    def test_empty_string(self):
        assert clean_text("") == ""


class TestStripHtml:
    def test_removes_tags(self):
        assert strip_html("<p>Hello</p>") == "Hello"

    def test_nested_tags(self):
        result = strip_html("<div><b>bold</b></div>")
        assert "bold" in result
        assert "<" not in result

    def test_no_tags_unchanged(self):
        assert strip_html("plain text") == "plain text"

    def test_non_string_returns_empty(self):
        assert strip_html(None) == ""  # type: ignore[arg-type]


class TestNormalize:
    def test_basic(self):
        assert normalize("  hello  ") == "hello"

    def test_strips_html_when_flag_set(self):
        assert normalize("<b>bold</b>", strip_html_tags=True) == "bold"

    def test_non_breaking_space_replaced(self):
        result = normalize("a\u00A0b")
        assert result == "a b"

    def test_zero_width_removed(self):
        result = normalize("a\u200bb")
        assert result == "ab"

    def test_bom_removed(self):
        result = normalize("\ufeffhello")
        assert result == "hello"

    def test_newlines_collapsed(self):
        result = normalize("line1\nline2\r\nline3")
        assert result == "line1 line2 line3"

    def test_non_string_returns_empty(self):
        assert normalize(None) == ""  # type: ignore[arg-type]


class TestNormalizeForId:
    def test_lowercase(self):
        assert normalize_for_id("Hello") == "hello"

    def test_removes_spaces(self):
        assert normalize_for_id("hello world") == "helloworld"

    def test_removes_symbols(self):
        assert normalize_for_id("hello-world!") == "helloworld"

    def test_keeps_korean(self):
        result = normalize_for_id("학교이름")
        assert result == "학교이름"

    def test_strips_html(self):
        result = normalize_for_id("<b>test</b>")
        assert result == "test"

    def test_non_string_returns_empty(self):
        assert normalize_for_id(None) == ""  # type: ignore[arg-type]


class TestCountTokens:
    def test_returns_int(self):
        msgs = [{"role": "user", "content": "hello world"}]
        result = count_tokens(msgs)
        assert isinstance(result, int)
        assert result > 0

    def test_empty_messages(self):
        assert count_tokens([]) == 0

    def test_multiple_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert count_tokens(msgs) >= 2

    def test_missing_keys_no_crash(self):
        msgs = [{}]
        result = count_tokens(msgs)
        assert isinstance(result, int)
