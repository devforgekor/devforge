#!/usr/bin/env python3.12
# Status: experimental
# Path: lib/prj/orchestrator.py — P-R-J 오케스트레이션. web_prj MCP 툴, webh CLI에서 호출
"""HCP-MAD 기반 P-R-J 오케스트레이션 (웹 검색·검증용).

3단계 구조 (arxiv 2604.09679 HCP-MAD):
  HCV — 이종 pair(DeepSeek/Qwen) 독립 실행, 합의 시 즉시 종료
  HPAD — 불일치 시 상호 비판. 종료조건 3: consensus / answer-exchange / deadlock
  ECT  — 미해결 시 adjudicator(Duck.ai)로 가중투표, 사용자에게 결정 제안

[WHY] web LLM은 호출당 21~34초(2026-09-29 실측). 매 라운드를 끝까지 도는 고정 라운드
방식은 비용이 2배 들고 정확도도 떨어진다(HCP-MAD: 1,977→3,951 토큰, 성능 하락).
따라서 합의 검사를 첫 단계에 두고, 이득이 없는 라운드는 아예 하지 않는다.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from lib.prj.serialize import render_brief
from lib.prj.verify import Claim, VerifyResult, verify_claim


class Engine(Protocol):
    """웹 LLM 엔진 어댑터. 추측 금지·추출 실패 시 명시적으로 실패를 반환해야 한다."""

    name: str

    def ask(self, prompt: str) -> str: ...


class Phase(str, Enum):
    HCV = "hcv"
    HPAD = "hpad"
    ECT = "ect"


class Outcome(str, Enum):
    CONSENSUS = "consensus"
    ANSWER_EXCHANGE = "answer_exchange"
    DEADLOCK = "deadlock"
    ADJUDICATED = "adjudicated"


@dataclass
class Round:
    """한 라운드의 P·R 위치. answer-exchange/deadlock 판정에 필요한 이력이다."""

    index: int
    p_answer: str
    r_answer: str
    p_verdict: str | None = None
    r_verdict: str | None = None

    def positions(self) -> tuple[str, str]:
        return (self.p_answer.strip(), self.r_answer.strip())


@dataclass
class PRJResult:
    task_id: str
    question: str
    outcome: Outcome
    phase_reached: Phase
    rounds: list[Round] = field(default_factory=list)
    claim: Claim | None = None
    verify: VerifyResult | None = None
    adjudication: dict[str, Any] | None = None
    elapsed_ms: int = 0
    escalated: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "question": self.question,
            "outcome": self.outcome.value,
            "phase_reached": self.phase_reached.value,
            "rounds": [asdict(r) for r in self.rounds],
            "claim": asdict(self.claim) if self.claim else None,
            "verify": asdict(self.verify) if self.verify else None,
            "adjudication": self.adjudication,
            "escalated": self.escalated,
            "elapsed_ms": self.elapsed_ms,
        }


def detect_answer_exchange(rounds: list[Round], threshold: int = 2) -> bool:
    """두 에이전트가 자리를 맞바꾸는 교대 패턴(AB→BA→AB). HCP-MAD η_e.

    [WHY] 자리를 맞바꾸면 같은 논거를 영원히 주고받는다. 라운드를 더 해도 정보가 늘지
    않으므로 즉시 중단하고 ECT로 넘긴다.
    [BUGFIX] 초기 구현이 첫 항을 기준으로 "이전 항과 맞바뀌었는가"만 검사해서 3라운드
    AB→BA→AB를 놓쳤다(단위 테스트가 잡음). HCP-MAD 정의는 인접한 모든 쌍이 교환
    관계인 교대이므로 연속 비교로 검사해야 한다.
    """
    if len(rounds) < threshold + 1:
        return False
    tail = rounds[-(threshold + 1) :]
    for prev, curr in zip(tail, tail[1:]):
        prev_p, prev_r = prev.positions()
        if curr.positions() != (prev_r, prev_p):
            return False
    return True


def detect_deadlock(rounds: list[Round], threshold: int = 2) -> bool:
    """연속 라운드 위치 불변. HCP-MAD η_d."""
    if len(rounds) < threshold:
        return False
    return all(r.positions() == rounds[-1].positions() for r in rounds[-threshold:])


def detect_consensus(last: Round) -> bool:
    """이종 pair의 판정이 일치하면 합의.

    [WHY] 문자열 유사도가 아니라 명시적 판정(confirm/contradict)을 쓴다. 2026-09-29 실측에서
    DeepSeek과 Qwen은 같은 사실(12월 1일)을 완전히 다른 형식으로 말했다 — 유사도는 오판하고
    판정은 정확했다. HCP-MAD HCV도 "rapid pairwise consensus"를 종료 신호로 쓴다.
    """
    return last.p_verdict == "confirm" and last.r_verdict == "confirm"


class PRJOrchestrator:
    def __init__(
        self,
        proposer: Engine,
        reviewer: Engine,
        adjudicator: Engine | None = None,
        max_rounds: int = 3,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        # [WHY] max_rounds=3 — MAD 원논문(iteration limit 3) + deepdive "after 3 failures
        # report and wait" 컨벤션. 더 돌려도 정확도 이득이 없다는 실측(HCP-MAD §4) 근거.
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        self.proposer = proposer
        self.reviewer = reviewer
        self.adjudicator = adjudicator
        self.max_rounds = max_rounds
        self._log = on_log or (lambda _: None)

    def _independence_check(self) -> None:
        # [WHY] 이종 pair가 전제다. 같은 모델을 P·R로 쓰면 합의가 무의미해진다
        # (HCP-MAD: homogeneous groups produce the echo-chamber effect).
        if self.proposer.name == self.reviewer.name:
            raise ValueError(
                f"proposer/reviewer must differ (got {self.proposer.name!r} twice) — "
                "homogeneous pairs echo-chamber and consensus becomes meaningless"
            )

    def _build_claim(self, question: str, answer: str) -> Claim:
        return Claim(question=question, answer=answer, sources=verify_claim(answer).sources)

    def run(self, task_id: str, question: str) -> PRJResult:
        self._independence_check()
        t0 = time.monotonic()
        rounds: list[Round] = []

        # ── HCV: 독립 실행 ──────────────────────────────────────────────
        self._log("HCV: proposer/reviewer 독립 실행")
        p_answer = self.proposer.ask(question)
        r_answer = self.reviewer.ask(question)
        r0 = Round(index=1, p_answer=p_answer, r_answer=r_answer)
        rounds.append(r0)

        result = self._evaluate(task_id, question, rounds, Phase.HCV, t0)
        if result:
            return result

        # ── HPAD: 상호 비판 ─────────────────────────────────────────────
        # [WARNING] 라운드당 호출을 4회(재반박 2 + 판정 2)로 두면 실측 6분이 걸린다
        # (2026-09-29 E2E: 360초). web LLM 호출이 21~34초씩이라 비용이 지수적이다.
        # 판정을 답변과 한 번에 받아 호출을 2회로 줄인다 — 답변 끝에 VERDICT 한 줄.
        for idx in range(2, self.max_rounds + 1):
            self._log(f"HPAD round {idx}/{self.max_rounds}: 상호 비판")
            prev = rounds[-1]
            p_text = self.proposer.ask(_rebuttal_prompt(question, prev, role="P"))
            r_text = self.reviewer.ask(_rebuttal_prompt(question, prev, role="R"))
            p_clean, p_verdict = _split_verdict(p_text)
            r_clean, r_verdict = _split_verdict(r_text)
            rounds.append(
                Round(
                    index=idx,
                    p_answer=p_clean,
                    r_answer=r_clean,
                    p_verdict=p_verdict,
                    r_verdict=r_verdict,
                )
            )
            result = self._evaluate(task_id, question, rounds, Phase.HPAD, t0)
            if result:
                return result

        # ── ECT: 에스컬레이션 ───────────────────────────────────────────
        if self.adjudicator is None:
            return PRJResult(
                task_id=task_id,
                question=question,
                outcome=Outcome.DEADLOCK,
                phase_reached=Phase.HPAD,
                rounds=rounds,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
            )

        self._log("ECT: adjudicator 가중투표 호출")
        return self._adjudicate(task_id, question, rounds, t0)

    def _evaluate(
        self,
        task_id: str,
        question: str,
        rounds: list[Round],
        phase: Phase,
        t0: float,
    ) -> PRJResult | None:
        """종료 조건 판정. 계속해야 하면 None.

        [WHY] consensus는 검증 통과를 요구한다. 2026-09-29 E2E에서 P·R이 합의했지만
        최종 답변이 반박문이라 출처 0건인 채 "합의"로 통과했다. 합의는 P·R의 동의일 뿐
        근거의 존재가 아니므로 verify까지 통과해야 종료할 수 있다.
        """
        last = rounds[-1]
        if detect_consensus(last):
            claim = self._build_claim(question, self._best_answer(rounds))
            v = verify_claim(claim.answer, question)
            if v.ok:
                self._log("HCV/HPAD: consensus + verify ok — 조기 종료")
                return self._finalize(
                    task_id, question, Outcome.CONSENSUS, phase, rounds, claim, t0
                )
            self._log(f"HCV/HPAD: consensus지만 검증 실패({','.join(v.errors)}) — 계속")
        if detect_answer_exchange(rounds):
            self._log("HPAD: answer exchange 감지 — 중단")
            return self._finalize(
                task_id, question, Outcome.ANSWER_EXCHANGE, phase, rounds, None, t0
            )
        if detect_deadlock(rounds):
            self._log("HPAD: deadlock 감지 — 중단")
            return self._finalize(task_id, question, Outcome.DEADLOCK, phase, rounds, None, t0)
        return None

    def _best_answer(self, rounds: list[Round]) -> str:
        """최종 후보로 쓸 답변을 고른다.

        [BUGFIX] 2026-09-29 E2E — 라운드 2의 P 답변은 "동의 …"로 시작하는 *반박문*이었고
        원 주장 대신 그것이 claim이 됐다. 반박문에는 원문의 출처가 옮겨 붙지 않아 검증이
        통과할 수 없다. 라운드가 쌓여도 **첫 라운드의 원 주장**을 우선 사용한다.
        """
        for rnd in rounds:
            v = verify_claim(rnd.p_answer)
            if v.ok:
                return rnd.p_answer
        return rounds[0].p_answer

    def _finalize(
        self,
        task_id: str,
        question: str,
        outcome: Outcome,
        phase: Phase,
        rounds: list[Round],
        claim: Claim | None,
        t0: float,
        adjudication: dict[str, Any] | None = None,
    ) -> PRJResult:
        verify = verify_claim(claim.answer, question) if claim else None
        # [WARNING] 검증 실패를 기록된 결과로 승격할 수 없다. E2E(2026-09-29)에서 P·R이
        # 합의했지만 최종 답변이 반박문("동의 … 상대방의 URL도 유효")이어서 출처가 0건이었고,
        # consensus로 통과해 "합의(미확정)"로 표시되었다. 합의와 검증 실패는 별개다 —
        # 검증 실패면 ECT로 넘겨 새 1차 출처를 찾게 한다.
        if outcome is Outcome.CONSENSUS and verify is not None and not verify.ok:
            outcome = Outcome.DEADLOCK
            claim = None
        return PRJResult(
            task_id=task_id,
            question=question,
            outcome=outcome,
            phase_reached=phase,
            rounds=rounds,
            claim=claim,
            verify=verify,
            adjudication=adjudication,
            escalated=phase is Phase.ECT,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
        )

    def _adjudicate(self, task_id: str, question: str, rounds: list[Round], t0: float) -> PRJResult:
        # [WARNING] adjudicator도 LLM이므로 확정에 쓰지 않는다. §8.2 — 3개 모델 합의는
        # 독립 검증이 아니다(4개 독립 인스턴스가 같은 오류에 56% 일치, 140× 위반).
        # 여기서는 "사용자에게 올릴 결정 제안"까지만 만든다. promote는 사람이 한다.
        assert self.adjudicator is not None
        transcript = "\n\n".join(
            f"--- Round {r.index} ---\nP: {r.p_answer}\nR: {r.r_answer}" for r in rounds
        )
        raw = self.adjudicator.ask(
            _adjudicator_prompt(question, transcript, [self.proposer.name, self.reviewer.name])
        )
        adjudication = _parse_adjudication(raw)
        winner = self._best_answer(rounds)
        claim = self._build_claim(question, winner)
        return self._finalize(
            task_id, question, Outcome.ADJUDICATED, Phase.ECT, rounds, claim, t0, adjudication
        )


def _rebuttal_prompt(question: str, prev: Round, role: str) -> str:
    # [WHY] 상대 주장을 구조화해 전달하되 전체 대화는 재주입하지 않는다. 전문 재주입은
    # industry 표준이 anti-pattern으로 규명됐다(§11.1) — 라운드가 늘면 컨텍스트가 선형 증가한다.
    # 판정을 답변과 한 번에 받기 위해 VERDICT 한 줄을 요구한다(라운드당 호출 4회→2회,
    # 실측 6분→약 3분. 2026-09-29 E2E).
    mine, theirs = (prev.p_answer, prev.r_answer) if role == "P" else (prev.r_answer, prev.p_answer)
    other = "R (Qwen)" if role == "P" else "P (DeepSeek)"
    return f"""질문: {question}

[내 이전 주장]
{mine}

[상대({other})의 주장 — 반박하거나 방어할 것]
{theirs}

먼저 답변을 쓰고, 마지막 줄에 아래 형식으로 판정을 남겨라.
VERDICT 뒤에 다른 텍스트를 쓰지 마라.

VERDICT: confirm | contradict | partial
(confirm=내 주장이 맞음, contradict=상대가 틀렸음, partial=일부만 맞음)"""


def _split_verdict(text: str) -> tuple[str, str | None]:
    """답변 끝의 VERDICT 한 줄을 떼어낸다. 없으면 None — 추측 금지(§0)."""
    match = re.search(
        r"VERDICT\s*[:：]\s*(confirm|contradict|partial)\s*$", text.strip(), re.I | re.M
    )
    if not match:
        return text.strip(), None
    return text[: match.start()].strip(), match.group(1).lower()


def _adjudicator_prompt(question: str, transcript: str, engines: list[str]) -> str:
    return f"""질문: {question}

[{engines[0]}와 {engines[1]}가 {len(transcript.split("--- Round")) - 1}라운드 동안 합의하지 못했다.]

전체 논쟁 기록:
{transcript}

판정하라. 반드시 아래 JSON만 출력하고 다른 텍스트는 쓰지 마라:
{{"verdict": "confirmed"|"refuted"|"inconclusive", "confidence": 0.0-1.0,
  "new_source": "1차 출처 URL 또는 빈 문자열", "reasoning": "근거 2문장"}}

중요: 두 모델이 합의하지 못한 이유를 설명해라. 확답을 만들지 마라.
합의에 도달하지 못한 것처럼 보이면 inconclusive을 선택하라."""


def _parse_adjudication(raw: str) -> dict[str, Any]:
    """판정 결과 파싱. 실패 시 명시적 실패를 반환 — 추측 금지(§0)."""
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        data = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError) as exc:
        return {
            "verdict": "inconclusive",
            "confidence": 0.0,
            "new_source": "",
            "parse_error": str(exc),
        }
    verdict = data.get("verdict", "inconclusive")
    if verdict not in ("confirmed", "refuted", "inconclusive"):
        verdict = "inconclusive"
    return {
        "verdict": verdict,
        "confidence": data.get("confidence", 0.0),
        "new_source": data.get("new_source", ""),
        "reasoning": data.get("reasoning", ""),
    }


def brief(result: PRJResult) -> str:
    return render_brief(result)
