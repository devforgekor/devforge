#!/usr/bin/env python3.12
# Status: experimental
# Path: lib/prj/serialize.py — lib.prj.orchestrator.brief 호출
"""PRJResult를 사람/에이전트가 읽을 형태로 직렬화.

[WHY] J(adjudicator)는 LLM이므로 확정에 쓰지 않는다. 따라서 brief는 항상 "무엇이 아직
미확정인지"를 명시한다 — 확정으로 읽히면 방어선 4(promote는 사람이 명시)가 무너진다.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from lib.prj.orchestrator import PRJResult

MAX_ANSWER_CHARS = 4000

OUTCOME_LABEL = {
    "consensus": "P-R 합의 (미확정)",
    "answer_exchange": "답 교환으로 중단",
    "deadlock": "교착 상태로 중단",
    "adjudicated": "판정 제안됨 (미확정)",
}


def render_brief(result: "PRJResult") -> str:
    lines = [
        f"──── P-R-J: {result.task_id} ────",
        f"질문: {result.question}",
        f"단계: {result.phase_reached} / 결과: {OUTCOME_LABEL.get(result.outcome.value, result.outcome.value)}",
        f"라운드: {len(result.rounds)}  |  소요: {result.elapsed_ms}ms",
    ]

    if result.verify:
        v = result.verify
        lines.append(f"출처: {len(v.sources)}건" if v.sources else "출처: 없음 — 검증 불가")
        if v.errors:
            lines.append(f"검증 실패: {', '.join(v.errors)}")

    if result.adjudication:
        adj = result.adjudication
        lines.extend(
            [
                f"판정 제안: {adj.get('verdict', 'inconclusive')} "
                f"(신뢰도 {adj.get('confidence', 0)})",
                f"근거: {adj.get('reasoning', '')}",
            ]
        )
        if adj.get("new_source"):
            lines.append(f"새 1차 출처: {adj['new_source']}")
    else:
        lines.append("판정 제안: 없음")

    if result.claim:
        lines.extend(["", "--- 최종 후보 답변 ---", result.claim.answer[:MAX_ANSWER_CHARS]])

    lines.extend(
        [
            "",
            "※ 이 결과는 확정되지 않았습니다. 1차 출처를 직접 확인한 뒤 decide 하십시오.",
        ]
    )
    return "\n".join(lines)


def render_json(result: "PRJResult") -> str:
    return json.dumps(result.to_json(), ensure_ascii=False, indent=2)
