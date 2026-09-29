#!/usr/bin/env python3.12
# Status: production
# Path: pytest 대상 — lib.prj.verify 결정론 검증
"""verify_claim은 LLM을 호출하지 않으므로 완전히 결정론적이다."""

import pytest
from lib.prj.verify import extract_sources, verify_claim


class TestShouldExtractUrlsWhenAnswerContainsThem:
    def test_should_extract_plain_url(self):
        assert extract_sources("출처: https://a.com/x") == ["https://a.com/x"]

    def test_should_deduplicate_preserving_order(self):
        assert extract_sources("https://a.com https://b.com https://a.com") == [
            "https://a.com",
            "https://b.com",
        ]

    def test_should_strip_trailing_punctuation(self):
        assert extract_sources("see https://a.com/x.") == ["https://a.com/x"]

    def test_should_strip_wrapping_parentheses(self):
        assert extract_sources("(https://a.com/x)") == ["https://a.com/x"]

    def test_should_return_empty_when_no_url(self):
        assert extract_sources("인용 없음") == []


class TestShouldRejectClaimWhenEvidenceMissing:
    def test_should_reject_when_no_sources(self):
        """실측된 최대 실패 모드: 출처명만 인용하고 URL을 넣지 않은 DeepSeek 답변."""
        result = verify_claim("2026년 예산은 9.9조원이다. (국가AI전략위)")
        assert result.ok is False
        assert "no_sources" in result.errors

    def test_should_reject_empty_answer(self):
        result = verify_claim("")
        assert result.ok is False
        assert "empty_answer" in result.errors

    def test_should_reject_citation_fragment_only(self):
        """각주 마커만 남은 파싱 실패 (web-llm-cli.sh is_valid_answer와 동일한 방어)."""
        result = verify_claim("-\n42", require_sources=False)
        assert result.ok is False
        assert "citation_fragment_only" in result.errors

    def test_should_accept_when_source_present(self):
        result = verify_claim("2025년 12월 1일 공개. https://a.com/news")
        assert result.ok is True
        assert result.sources == ["https://a.com/news"]


class TestShouldWarnWhenQuestionMissing:
    def test_should_warn_but_not_fail(self):
        result = verify_claim("답변 https://a.com")
        assert result.ok is True
        assert "missing_question" in result.warnings


class TestShouldAllowSourcesOptionalWhenExplicitlyDisabled:
    def test_should_pass_without_sources_when_not_required(self):
        result = verify_claim("검색 없이 답한 경우", require_sources=False)
        assert result.ok is True
        assert result.errors == []


class TestShouldStayDeterministicWhenCalledRepeatedly:
    @pytest.mark.parametrize("text", ["https://a.com", "인용 없음", "-\n2", ""])
    def test_should_return_identical_result_for_same_input(self, text):
        assert verify_claim(text).to_dict() == verify_claim(text).to_dict()
