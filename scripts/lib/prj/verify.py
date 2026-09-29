#!/usr/bin/env python3.12
# Status: experimental
# Path: lib/prj/orchestrator.py, lib/prj/verify.py 자체 테스트에서 사용
"""결정론적 주장 검증 — LLM을 믿지 않고 구조를 검사한다.

[WHY] web LLM은 출처 URL 없이도 그럴듯한 답변을 만든다(2026-09-29 실측: DeepSeek이
출처명만 인용하고 URL을 넣지 않은 채 promote가 통과). 모델이 스스로 sources를 생성하게
하면 없는 URL을 지어낼 수 있으므로, 원문에서만 추출하고 그것의 부재를 실패로 친다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# [WARNING] 부정 브래킷 안에서 \[ \] 를 쓰면 이 환경의 grep/sed가 확장 브래킷 표현식으로
# 오인해 매칭이 0건이 된다(handoff.sh에서 실측). Python re는 영향 없지만 동일한 실수를
# 반복하지 않도록 문자 클래스를 단순하게 유지한다.
_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}]+")
_TRAILING = ".,;:!?)]}'\""

REQUIRED_FIELDS = ("question", "answer")


@dataclass
class Claim:
    question: str
    answer: str
    sources: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    ok: bool
    sources: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sources": self.sources,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def extract_sources(text: str) -> list[str]:
    """원문에서 URL만 추출. 중복 제거, 순서 보존."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(_TRAILING)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def verify_claim(
    answer: str,
    question: str = "",
    *,
    require_sources: bool = True,
    min_answer_chars: int = 2,
) -> VerifyResult:
    """주장을 검증한다. 모델을 호출하지 않으므로 완전히 결정론적이다."""
    errors: list[str] = []
    warnings: list[str] = []

    if not (answer or "").strip():
        errors.append("empty_answer")
    elif len(answer.strip()) < min_answer_chars:
        errors.append("answer_too_short")

    sources = extract_sources(answer)
    if require_sources and not sources:
        # [WARNING] 가장 자주 발생하는 실제 실패. E2E에서 이 상태로 promote가 통과해
        # 검증 불가능한 주장이 신뢰 저장소에 들어갔었다. 반드시 실패로 취급한다.
        errors.append("no_sources")

    stripped = re.sub(r"\s+", "", answer or "")
    if re.fullmatch(r"-\d+", stripped):
        # 각주 인용 마커만 남은 경우 (web-llm-cli.sh의 is_valid_answer와 같은 방어).
        errors.append("citation_fragment_only")

    if not question.strip():
        warnings.append("missing_question")

    return VerifyResult(ok=not errors, sources=sources, errors=errors, warnings=warnings)
