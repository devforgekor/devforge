#!/usr/bin/env python3
# Status: experimental
# Path: none — Red phase test for truncate_at_boundary()
"""Tests for text_quality.truncate_at_boundary() — sentence-aware Korean truncation."""

import sys
from pathlib import Path

import pytest

# ensure scripts/ is importable
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from lib.text_quality import truncate_at_boundary


# ── Happy path ─────────────────────────────────────────────────────────

def test_shorter_than_max_returns_as_is():
    """Text under max_chars returns unchanged."""
    text = "안녕하세요."
    assert truncate_at_boundary(text, 50) == text


def test_ends_at_sentence_boundary():
    """Exact sentence boundary — no truncation needed."""
    text = "첫 번째 문장입니다. 두 번째 문장입니다."
    result = truncate_at_boundary(text, 30)
    assert result == text


# ── Sentence boundary truncation ────────────────────────────────────────

def test_truncate_at_korean_period():
    """Mid-sentence → truncate at previous period."""
    text = "첫 번째 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다."
    result = truncate_at_boundary(text, 20)
    assert len(result) <= 20
    assert result == "첫 번째 문장입니다."


def test_truncate_at_korean_period_fits_two():
    """When two sentences fit within max_chars, both are kept."""
    text = "첫 번째 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다."
    result = truncate_at_boundary(text, 25)
    assert len(result) <= 25
    assert result == "첫 번째 문장입니다. 두 번째 문장입니다."


def test_truncate_with_question_mark():
    """Question mark is a valid sentence boundary."""
    text = "이게 맞나요? 저는 잘 모르겠습니다."
    result = truncate_at_boundary(text, 15)
    # "이게 맞나요? " = 8 chars → result should be "이게 맞나요?"
    assert len(result) <= 15
    assert result == "이게 맞나요?"


def test_truncate_with_exclamation():
    """Exclamation mark is a valid sentence boundary."""
    text = "와! 대박이다. 정말?"
    # "와! " = 3 chars, "대박이다. " = 6 chars, "정말?" = 4 chars
    # with max_chars=10: "와! 대박이다." = 10 chars exactly
    result = truncate_at_boundary(text, 10)
    assert len(result) <= 10
    assert result == "와! 대박이다."


def test_truncate_with_korean_ellipsis():
    """Korean ellipsis … is a sentence boundary."""
    text = "생각 중입니다… 아직 결정 못 했어요."
    result = truncate_at_boundary(text, 20)
    assert len(result) <= 20
    assert result == "생각 중입니다…"


# ── Fallback: word boundary ─────────────────────────────────────────────

def test_fallback_word_boundary():
    """No sentence boundary → truncate at last space."""
    text = "이것은 문장 부호 없이 공백만 있는 긴 문자열입니다"
    result = truncate_at_boundary(text, 20)
    assert len(result) <= 20
    # Should end at a space boundary
    assert not result.endswith(" ")


def test_fallback_no_boundary_hard_cut():
    """No sentence or word boundary → hard truncate at max_chars."""
    text = "가나다라마바사아자차카타파하"
    result = truncate_at_boundary(text, 5)
    assert len(result) == 5


# ── Edge cases ──────────────────────────────────────────────────────────

def test_empty_string():
    """Empty string returns empty."""
    assert truncate_at_boundary("", 100) == ""


def test_none_input():
    """None input returns empty string."""
    assert truncate_at_boundary(None, 100) == ""  # type: ignore[arg-type]


def test_zero_max_chars():
    """max_chars=0 returns empty string."""
    assert truncate_at_boundary("hello world", 0) == ""


def test_max_chars_greater_than_text_length():
    """max_chars > len(text) returns text as-is."""
    text = "짧은 문장."
    assert truncate_at_boundary(text, 100) == text


def test_whitespace_only():
    """Whitespace-only input returns empty string."""
    assert truncate_at_boundary("   ", 100) == ""
    assert truncate_at_boundary("\n\t", 100) == ""


def test_multiple_boundaries_chooses_last_valid():
    """Truncate at the LAST sentence boundary that fits within max_chars."""
    text = "Short. Medium length sentence. This is the longest one."
    result = truncate_at_boundary(text, 35)
    # "Short. " = 7 chars, "Medium length sentence. " = 25 chars
    # "Short. Medium length sentence." = 7 + 24 = 31 chars
    # "Short. Medium length sentence. This" would exceed 35
    # So should pick "Short. Medium length sentence."
    assert len(result) <= 35
    assert result == "Short. Medium length sentence."


def test_period_followed_by_space_is_boundary():
    """Period + space triggers boundary (including after abbreviations).

    Note: Current implementation does NOT distinguish abbreviation
    periods (e.g., "Ph.D.") from sentence-ending periods. A period
    followed by space is always treated as a sentence boundary.
    """
    text = "Ph.D. candidate is here. And more text."
    result = truncate_at_boundary(text, 15)
    assert len(result) <= 15
    assert result == "Ph.D."
