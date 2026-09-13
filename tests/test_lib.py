#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_lib.py — pytest (통합)
"""Library tests: text quality, operator model evaluation.

Usage:  python3 -m pytest tests/test_lib.py -v
        python3 tests/test_lib.py
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import pytest
except ImportError:
    pytest = None


# ═══════════════════════════════════════════════════════════════════════════
# text_quality tests (proper pytest)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(pytest is None, reason="pytest not available")
class TestTextQuality:
    """Tests for text_quality.truncate_at_boundary() — sentence-aware Korean truncation."""

    def test_shorter_than_max_returns_as_is(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("안녕하세요.", 50) == "안녕하세요."

    def test_ends_at_sentence_boundary(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("첫 번째 문장입니다. 두 번째 문장입니다.", 30) == "첫 번째 문장입니다. 두 번째 문장입니다."

    def test_truncate_at_korean_period(self):
        from notice.lib.text_quality import truncate_at_boundary
        text = "첫 번째 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다."
        result = truncate_at_boundary(text, 20)
        assert len(result) <= 20
        assert result == "첫 번째 문장입니다."

    def test_truncate_at_korean_period_fits_two(self):
        from notice.lib.text_quality import truncate_at_boundary
        text = "첫 번째 문장입니다. 두 번째 문장입니다. 세 번째 문장입니다."
        result = truncate_at_boundary(text, 25)
        assert len(result) <= 25
        assert result == "첫 번째 문장입니다. 두 번째 문장입니다."

    def test_truncate_with_question_mark(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("이게 맞나요? 저는 잘 모르겠습니다.", 15) == "이게 맞나요?"

    def test_truncate_with_exclamation(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("와! 대박이다. 정말?", 10) == "와! 대박이다."

    def test_truncate_with_korean_ellipsis(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("생각 중입니다… 아직 결정 못 했어요.", 20) == "생각 중입니다…"

    def test_fallback_word_boundary(self):
        from notice.lib.text_quality import truncate_at_boundary
        text = "이것은 문장 부호 없이 공백만 있는 긴 문자열입니다"
        result = truncate_at_boundary(text, 20)
        assert len(result) <= 20
        assert not result.endswith(" ")

    def test_fallback_no_boundary_hard_cut(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert len(truncate_at_boundary("가나다라마바사아자차카타파하", 5)) == 5

    def test_empty_string(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("", 100) == ""

    def test_none_input(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary(None, 100) == ""

    def test_zero_max_chars(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("hello world", 0) == ""

    def test_max_chars_greater_than_text_length(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("짧은 문장.", 100) == "짧은 문장."

    def test_whitespace_only(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("   ", 100) == ""
        assert truncate_at_boundary("\n\t", 100) == ""

    def test_multiple_boundaries_chooses_last_valid(self):
        from notice.lib.text_quality import truncate_at_boundary
        text = "Short. Medium length sentence. This is the longest one."
        result = truncate_at_boundary(text, 35)
        assert len(result) <= 35
        assert result == "Short. Medium length sentence."

    def test_period_followed_by_space_is_boundary(self):
        from notice.lib.text_quality import truncate_at_boundary
        assert truncate_at_boundary("Ph.D. candidate is here. And more text.", 15) == "Ph.D."


# ═══════════════════════════════════════════════════════════════════════════
# Operator model evaluation
# ═══════════════════════════════════════════════════════════════════════════

LLM_ENDPOINT = os.getenv("OPERATOR_ENDPOINT", "http://127.0.0.1:8081/v1/chat/completions")

SYSTEM_PROMPT = """You are the DevForge OPERATOR. Given a user request, choose the right specialist mode:

- generate → standalone code generation (30B)
- debate → 30B Draft + 3B Reviewer
- review → 30B Draft + 14B Review
- verify → 27B final verification
- general → quick Q&A

Return JSON: {"mode":"generate|debate|review|verify|general","reasoning":"...","confidence":0-100}"""

TEST_CASES = [
    "Write a Python function to sort a list of dictionaries by a key.",
    "Review this code for security vulnerabilities: def login(pwd): return pwd == 'admin123'",
    "What's the capital of France?",
    "Compare two approaches to database migration and pick the best one.",
    "Debug why this SQL query is slow: SELECT * FROM orders JOIN customers ON...",
]


def test_operator_smoke():
    """Quick operator dispatch test (8 test cases)."""
    for i, query in enumerate(TEST_CASES[:8]):
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": query}]
        body = {"messages": messages, "max_tokens": 128, "temperature": 0.1}
        try:
            req = urllib.request.Request(LLM_ENDPOINT, data=json.dumps(body).encode(),
                                          headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            parsed = json.loads(content) if content.startswith("{") else {}
            mode = parsed.get("mode", "?")
            conf = parsed.get("confidence", "?")
            print(f"  [{i+1}] {query[:50]:<50} → mode={mode} conf={conf}")
        except Exception as e:
            print(f"  [{i+1}] {query[:50]:<50} → ERROR: {e}")


if __name__ == "__main__":
    print("=== text_quality tests ===")
    t = TestTextQuality()
    for method_name in dir(t):
        if method_name.startswith("test_"):
            getattr(t, method_name)()
            print(f"  {method_name}: OK")
    print("\n=== operator test ===")
    test_operator_smoke()
