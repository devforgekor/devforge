#!/usr/bin/env python3.12
# Status: production
# Path: pytest 대상 — lib.prj.orchestrator HCP-MAD 3단계 상태 전이
"""종료 조건(HCP-MAD HCV/HPAD)과 방어선을 검증한다. 엔진은 전부 fake라 LLM 호출이 없다."""

import pytest
from lib.prj.orchestrator import (
    Outcome,
    Phase,
    PRJOrchestrator,
    Round,
    _clip,
    _rebuttal_prompt,
    _split_verdict,
    detect_answer_exchange,
    detect_consensus,
    detect_deadlock,
)


class FakeEngine:
    """스크립트화된 엔진. 호출 순서를 어서션할 수 있게 한다."""

    def __init__(self, name, answers=None, verdicts=None, p_verdicts=None):
        self.name = name
        self.answers = list(answers or ["기본 답변 https://a.com"])
        self.verdicts = list(verdicts or [])
        # [WHY] proposer/reviewer가 같은 큐를 쓰면 먼저 호출된 쪽이 값을 소진해
        # 라운드 판정이 의도와 다르게 된다. 역할별로 분리한다.
        self.p_verdicts = list(p_verdicts or [])
        self.calls = []

    def ask(self, prompt):
        self.calls.append(prompt)
        if "VERDICT" in prompt:
            text = self.answers.pop(0) if self.answers else "반복 답변 https://a.com"
            if self.p_verdicts and "[내 이전 주장]" in prompt:
                v = self.p_verdicts.pop(0)
            else:
                v = self.verdicts.pop(0) if self.verdicts else "partial"
            return f"{text}\n\nVERDICT: {v}"
        if "verdict" in prompt.lower() or "판정해라" in prompt:
            return self.verdicts.pop(0) if self.verdicts else "partial"
        return self.answers.pop(0) if self.answers else "반복 답변 https://a.com"


def _round(i, p, r, pv=None, rv=None):
    return Round(index=i, p_answer=p, r_answer=r, p_verdict=pv, r_verdict=rv)


class TestShouldRejectHomogeneousPairWhenSameEngineGiven:
    def test_should_raise_when_proposer_equals_reviewer(self):
        engine = FakeEngine("deepseek")
        orch = PRJOrchestrator(proposer=engine, reviewer=FakeEngine("deepseek"))
        with pytest.raises(ValueError, match="echo-chamber"):
            orch.run("t", "질문")


class TestShouldStopEarlyWhenConsensusReached:
    def test_should_finish_in_hcv_without_adjudicator(self):
        p = FakeEngine("deepseek", ["P 답변 https://a.com"])
        r = FakeEngine("qwen", ["R 답변 https://b.com"], verdicts=["confirm"])
        orch = PRJOrchestrator(p, r, adjudicator=None)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("lib.prj.orchestrator.detect_consensus", lambda last: True)
            result = orch.run("t1", "질문")
        assert result.outcome is Outcome.CONSENSUS
        assert result.phase_reached is Phase.HCV
        assert result.escalated is False

    def test_should_not_call_adjudicator_when_consensus(self):
        p = FakeEngine("deepseek", ["P https://a.com"] * 5)
        r = FakeEngine("qwen", ["R https://b.com"] * 5, verdicts=["confirm"] * 5)
        adj = FakeEngine("duckai", ["{}"])
        orch = PRJOrchestrator(p, r, adjudicator=adj)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("lib.prj.orchestrator.detect_consensus", lambda last: True)
            orch.run("t2", "질문")
        assert adj.calls == []


class TestShouldDetectAnswerExchangeWhenPositionsSwap:
    def test_should_detect_ab_pattern(self):
        rounds = [
            _round(1, "A1", "B1"),
            _round(2, "B1", "A1"),
            _round(3, "A1", "B1"),
        ]
        assert detect_answer_exchange(rounds) is True

    def test_should_not_detect_when_positions_progress(self):
        rounds = [
            _round(1, "A1", "B1"),
            _round(2, "A2", "B2"),
            _round(3, "A3", "B3"),
        ]
        assert detect_answer_exchange(rounds) is False

    def test_should_not_detect_below_threshold(self):
        assert detect_answer_exchange([_round(1, "A", "B")]) is False


class TestShouldDetectDeadlockWhenPositionsFrozen:
    def test_should_detect_unchanged_positions(self):
        rounds = [_round(i, "same", "same") for i in (1, 2, 3)]
        assert detect_deadlock(rounds) is True

    def test_should_not_detect_single_round(self):
        assert detect_deadlock([_round(1, "A", "B")]) is False

    def test_should_not_detect_when_progressing(self):
        rounds = [_round(1, "A1", "B1"), _round(2, "A2", "B2"), _round(3, "A3", "B3")]
        assert detect_deadlock(rounds) is False


class TestShouldRequireBothConfirmWhenCheckingConsensus:
    def test_should_true_when_both_confirm(self):
        assert detect_consensus(_round(1, "A", "B", "confirm", "confirm")) is True

    def test_should_false_when_one_contradicts(self):
        assert detect_consensus(_round(1, "A", "B", "confirm", "contradict")) is False

    def test_should_false_when_verdict_missing(self):
        assert detect_consensus(_round(1, "A", "B")) is False


class TestShouldEscalateToAdjudicatorWhenAllRoundsDisagree:
    def test_should_reach_ect_and_never_auto_confirm(self):
        # [WHY] 라운드마다 서로 다른 답이어야 deadlock/answer-exchange가 걸리지 않아
        # ECT까지 도달한다. 같은 답을 돌려주면 조기 종료 조건이 먼저 발동한다(의도된 동작).
        p = FakeEngine("deepseek", [f"P 주장 {i} https://a.com/{i}" for i in range(8)])
        r = FakeEngine(
            "qwen", [f"R 주장 {i} https://b.com/{i}" for i in range(8)], verdicts=["contradict"] * 8
        )
        adj = FakeEngine(
            "duckai",
            [
                '{"verdict":"confirmed","confidence":0.9,"new_source":"https://o.com","reasoning":"ok"}'
            ],
        )
        orch = PRJOrchestrator(p, r, adjudicator=adj, max_rounds=3)
        result = orch.run("t3", "질문")
        assert result.outcome is Outcome.ADJUDICATED
        assert result.phase_reached is Phase.ECT
        assert result.escalated is True
        # [WARNING] adjudicator 판정은 제안일 뿐 — confirmed여도 자동 승격되지 않는다.
        assert not hasattr(result, "promoted_at")

    def test_should_never_call_adjudicator_when_absent(self):
        p = FakeEngine("deepseek", [f"P {i} https://a.com/{i}" for i in range(8)])
        r = FakeEngine(
            "qwen", [f"R {i} https://b.com/{i}" for i in range(8)], verdicts=["contradict"] * 8
        )
        orch = PRJOrchestrator(p, r, adjudicator=None, max_rounds=3)
        result = orch.run("t4", "질문")
        assert result.phase_reached is Phase.HPAD
        assert result.outcome is Outcome.DEADLOCK


class TestShouldFailClosedWhenAdjudicatorReturnsGarbage:
    def test_should_downgrade_unparseable_verdict_to_inconclusive(self):
        p = FakeEngine("deepseek", [f"P {i} https://a.com/{i}" for i in range(8)])
        r = FakeEngine(
            "qwen", [f"R {i} https://b.com/{i}" for i in range(8)], verdicts=["contradict"] * 8
        )
        adj = FakeEngine("duckai", ["완전한 쓰레기값"])
        orch = PRJOrchestrator(p, r, adjudicator=adj, max_rounds=3)
        result = orch.run("t5", "질문")
        assert result.adjudication["verdict"] == "inconclusive"
        assert "parse_error" in result.adjudication

    def test_should_reject_unknown_verdict_value(self):
        p = FakeEngine("deepseek", [f"P {i} https://a.com/{i}" for i in range(8)])
        r = FakeEngine(
            "qwen", [f"R {i} https://b.com/{i}" for i in range(8)], verdicts=["contradict"] * 8
        )
        adj = FakeEngine("duckai", ['{"verdict":"totally_made_up","confidence":0.99}'])
        orch = PRJOrchestrator(p, r, adjudicator=adj, max_rounds=3)
        result = orch.run("t6", "질문")
        assert result.adjudication["verdict"] == "inconclusive"


class TestShouldCarrySourceUrlsWhenClaimFinalized:
    def test_should_extract_sources_from_final_answer(self):
        p = FakeEngine("deepseek", ["정답은 12월 1일. https://a.com/news"])
        r = FakeEngine("qwen", ["12월 1일. https://b.com/news"], verdicts=["confirm"])
        orch = PRJOrchestrator(p, r, adjudicator=None)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("lib.prj.orchestrator.detect_consensus", lambda last: True)
            result = orch.run("t7", "질문")
        assert "https://a.com/news" in result.claim.sources
        assert result.verify.ok is True


class TestShouldRejectConsensusWhenVerificationFails:
    """E2E 회귀 (2026-09-29): P·R이 합의했지만 최종 답변이 반박문이라 출처 0건이었다.

    consensus는 P·R의 동의일 뿐 근거의 존재가 아니므로, verify까지 통과해야 종료한다.
    """

    def test_should_not_finalize_as_consensus_when_answer_has_no_sources(self):
        p = FakeEngine("deepseek", ["동의합니다. 상대방이 옳습니다."])
        r = FakeEngine("qwen", ["공식 확인했습니다."], verdicts=["confirm"])
        orch = PRJOrchestrator(p, r, adjudicator=None, max_rounds=1)
        result = orch.run("t8", "질문")
        assert result.outcome is not Outcome.CONSENSUS
        assert result.claim is None

    def test_should_escalate_to_ect_when_consensus_fails_verification(self):
        p = FakeEngine("deepseek", [f"P{i} https://a.com/{i}" for i in range(6)])
        r = FakeEngine(
            "qwen", [f"R{i} https://b.com/{i}" for i in range(6)], verdicts=["partial", "partial"]
        )
        adj = FakeEngine("duckai", ['{"verdict":"confirmed","confidence":0.8,"new_source":""}'])
        orch = PRJOrchestrator(p, r, adjudicator=adj, max_rounds=2)
        result = orch.run("t9", "질문")
        assert result.escalated is True


class TestShouldPreferOriginalClaimWhenLaterRoundIsRebuttal:
    """E2E 회귀 (2026-09-29): 라운드 2의 P 답변이 '동의 …' 반박문이라 claim이 됐다."""

    def test_should_use_first_valid_round_answer_not_latest_rebuttal(self):
        original = "공개일은 2025-12-01. https://api-docs.deepseek.com/news251201"
        rebuttal = "동의. 상대방의 주장이 타당합니다."
        p = FakeEngine("deepseek", [original, rebuttal], p_verdicts=["confirm"])
        # [WHY] 라운드 2에서 P·R 모두 confirm이어야 consensus 경로로 들어간다.
        # reviewer 큐는 호출 순서대로 소진되므로 [partial, confirm]으로 배치한다.
        r = FakeEngine("qwen", [f"R{i} https://b.com/{i}" for i in range(4)], verdicts=["confirm"])
        orch = PRJOrchestrator(p, r, adjudicator=None, max_rounds=2)
        result = orch.run("t10", "질문")
        assert result.claim is not None
        assert "api-docs.deepseek.com" in result.claim.answer


class TestShouldSplitVerdictWhenPromptRequestsIt:
    def test_should_extract_trailing_verdict_line(self):
        body, verdict = _split_verdict("답변 본문\n\nVERDICT: confirm")
        assert body == "답변 본문"
        assert verdict == "confirm"

    def test_should_return_none_when_no_verdict_line(self):
        body, verdict = _split_verdict("답변만 있음")
        assert verdict is None
        assert body == "답변만 있음"

    def test_should_reduce_call_count_per_round(self):
        """라운드당 호출 4회→2회 (실측 6분 → 약 3분)."""
        p = FakeEngine("deepseek", [f"P{i} https://a.com/{i}" for i in range(6)])
        r = FakeEngine(
            "qwen", [f"R{i} https://b.com/{i}" for i in range(6)], verdicts=["partial"] * 6
        )
        orch = PRJOrchestrator(p, r, adjudicator=None, max_rounds=2)
        orch.run("t11", "질문")
        assert len(p.calls) == 2
        assert len(r.calls) == 2


class TestShouldClipContextWhenAboveBrowserLimit:
    """실측(2026-09-29): 브라우저 타이핑은 55.8k자 통과 / 58.5k자 실패(입력 잘림).

    재논쟁 라운드는 P+R 답변을 함께 싣기 때문에 원 질문보다 크게 커진다.
    """

    def test_should_not_clip_when_below_limit(self):
        text = "짧은 주장 " * 100
        assert _clip(text) == text

    def test_should_clip_when_above_limit(self):
        big = "가" * 80_000
        out = _clip(big)
        assert len(out) < len(big)
        assert "중간" in out and "생략" in out

    def test_should_preserve_head_and_tail(self):
        big = "HEAD" + ("x" * 80_000) + "TAIL"
        out = _clip(big)
        assert out.startswith("HEAD")
        assert out.endswith("TAIL")

    def test_should_keep_rebuttal_prompt_under_limit(self):
        prev = Round(
            index=1,
            p_answer="P" * 40_000,
            r_answer="R" * 40_000,
        )
        prompt = _rebuttal_prompt("질문 " * 10_000, prev, role="P")
        assert len(prompt) < 60_000
