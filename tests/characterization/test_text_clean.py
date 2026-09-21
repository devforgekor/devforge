"""Characterization: text_clean language detection (Week 2, test 5).

Captures the CURRENT behavior of lib.text_cleaner.detect_language so the
refactor can prove it did not change. Behavior (2026-09-21):
  - empty/whitespace -> ("unknown", 0.0)
  - only 'ko'/'en' pass through, with a FIXED confidence of 0.8 (not langdetect's)
  - every other language (e.g. Japanese) is folded to ("unknown", 0.0)
"""

import pytest

pytestmark = pytest.mark.characterization

KOREAN = "안녕하세요. 오늘 날씨가 정말 좋습니다. 내일은 비가 온다고 합니다."
ENGLISH = "Hello, this is a simple English sentence about the weather today."
JAPANESE = "これは日本語のテキストです。今日はとても良い天気です。"


def test_empty_is_unknown():
    from lib.text_cleaner import detect_language

    assert detect_language("   ") == ("unknown", 0.0)


def test_korean_detected_with_fixed_confidence():
    from lib.text_cleaner import detect_language

    assert detect_language(KOREAN) == ("ko", 0.8)


def test_english_detected_with_fixed_confidence():
    from lib.text_cleaner import detect_language

    assert detect_language(ENGLISH) == ("en", 0.8)


def test_other_language_folds_to_unknown():
    from lib.text_cleaner import detect_language

    assert detect_language(JAPANESE) == ("unknown", 0.0)
