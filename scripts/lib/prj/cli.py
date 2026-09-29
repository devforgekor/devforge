#!/usr/bin/env python3.12
# Status: experimental
# Path: lib.prj.cli — prj run/status/brief/decide
"""웹 LLM 엔진 어댑터 + prj CLI.

[WHY] 브라우저 세션은 사용자 자산이라 저장소가 아니라 ~/.local/share 에 있다. 저장소는
오케스트레이션만 소유하고 세션은 어댑터가 참조한다(chrome_ingest.py precedent).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from typing import Any

from lib.prj.orchestrator import PRJOrchestrator, PRJResult
from lib.prj.serialize import render_brief

WEBLLM_SCRIPTS = os.environ.get(
    "WEBLLM_SCRIPTS", os.path.expanduser("~/.local/share/chrome-web-llm/scripts")
)
WEBLLM_CLI = os.path.join(WEBLLM_SCRIPTS, "web-llm.sh")
HANDOFF = os.path.join(WEBLLM_SCRIPTS, "handoff.sh")
STATE_DIR = os.environ.get("PRJ_STATE_DIR", os.path.expanduser("~/.local/share/chrome-web-llm/prj"))

TIMEOUT_WARN = 300
# [WHY] 장문 map-reduce는 청크마다 웹 호출을 반복한다. 162,066자 20청크 실측 4분.
# 질문 1회(300초)로는 부족하므로 파일 경로에는 더 긴 상한을 둔다.
CHUNK_TIMEOUT_WARN = 1800
# 브라우저 타이핑 실측 경계(2026-09-29): 55.8k자 통과 / 58.5k자 실패. 상한은 여유를 둔다.
MAX_CONTEXT_CHARS = 50_000


class EngineError(RuntimeError):
    """엔진 호출 실패. 추측·빈 문자열을 절대 반환하지 않는다(§0)."""


def _require(path: str, hint: str) -> str:
    if not os.path.isfile(path):
        raise EngineError(f"not found: {path} — {hint}")
    return path


class WebLLMEngine:
    """chrome-web-llm 브라우저 세션 (DeepSeek/Qwen).

    [WARNING] 단일 Chromium 탭을 공유한다. 병렬 호출은 탭 경쟁을 일으키므로
    직렬로만 호출할 것(§7.6).
    """

    def __init__(
        self,
        model: str,
        *,
        think: bool = True,
        timeout: int = TIMEOUT_WARN,
        file: str | None = None,
        chunk: bool = False,
    ) -> None:
        self.name = model
        self.model = model
        self.think = think
        self.timeout = timeout
        # [WHY] 장문은 argv로 넘기지 않는다. 252KB는 ARG_MAX로 즉시 죽고, 58k자를 넘으면
        # 브라우저 타이핑이 잘려 5회 재시도 후 실패한다(2026-09-29 실측).
        self.file = file
        self.chunk = chunk

    def ask(self, prompt: str) -> str:
        _require(WEBLLM_CLI, "web-llm.sh — ~/.local/share/chrome-web-llm/scripts")
        cmd = [WEBLLM_CLI, "-m", self.model, "--search", "--no-capture"]
        if self.think:
            cmd.append("--think")
        if self.file:
            cmd += ["-f", self.file]
            if self.chunk:
                cmd.append("--chunk")
        else:
            cmd += ["-c", prompt]
        cmd += ["--", prompt]
        # [WHY] 장문 map-reduce는 청크마다 웹 호출을 반복한다. 파일 하나가 수 분 걸리므로
        # 여유를 둔다(argv 질문 경로는 300초면 충분하다).
        if self.file:
            self.timeout = max(self.timeout, CHUNK_TIMEOUT_WARN)
        try:
            proc = subprocess.run(  # noqa: S603
                cmd, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError(f"{self.name}: timeout {self.timeout}s") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
            raise EngineError(f"{self.name}: exit {proc.returncode} — {tail[0]}")
        text = (proc.stdout or "").strip()
        if not text:
            raise EngineError(f"{self.name}: empty answer")
        return text


class DuckAIEngine:
    """Duck.ai warm session (프록시 경유, 캐시로 2회부터 비용 급감)."""

    def __init__(self, timeout: int = TIMEOUT_WARN) -> None:
        self.name = "duckai"
        self.timeout = timeout

    def ask(self, prompt: str) -> str:
        script = "/opt/projects/server/scripts/cli.py"
        _require(script, "cli.py — /opt/projects/server/scripts")
        try:
            proc = subprocess.run(  # noqa: S603
                ["python3.11", script, "research", "ask", prompt, "--fresh"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError(f"duckai: timeout {self.timeout}s") from exc
        if proc.returncode != 0:
            raise EngineError(f"duckai: exit {proc.returncode}")
        text = (proc.stdout or "").strip()
        if not text:
            raise EngineError("duckai: empty answer")
        return text


def _state_path(task_id: str) -> str:
    return os.path.join(STATE_DIR, f"{task_id}.json")


def _persist(result: PRJResult) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    path = _state_path(result.task_id)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_json(), fh, ensure_ascii=False, indent=2)
    return path


def _load(task_id: str) -> dict[str, Any]:
    path = _state_path(task_id)
    if not os.path.isfile(path):
        raise EngineError(f"no such task: {task_id}")
    with open(path, encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


def _push_handoff(result: PRJResult) -> None:
    """handoff.sh에 P의 최종 후보를 등록한다. 실패해도 PRJ 결과 자체는 유지된다."""
    if not result.claim:
        return
    try:
        proc = subprocess.run(  # noqa: S603
            [
                HANDOFF,
                "save",
                "--task",
                result.task_id,
                "--engine",
                "deepseek",
                "--question",
                result.question,
                "--answer",
                result.claim.answer,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if proc.returncode != 0:
        print(f"[warn] handoff 저장 실패: {(proc.stderr or '').strip()}", file=sys.stderr)
        return

    # [BUGFIX] 2026-09-29 E2E — claim만 저장해서 handoff.consensus이 항상 false로 남았고,
    # 그 결과 `prj decide`가 "P/R 합의 없음"으로 계속 거부되었다(사람이 판정할 수 없음).
    # orchestrator의 P-R 합의를 handoff에 반영해 두 시스템이 상태를 공유하게 한다.
    if result.outcome.value == "consensus":
        rev = subprocess.run(  # noqa: S603
            [
                HANDOFF,
                "review",
                "--task",
                result.task_id,
                "--engine",
                "qwen",
                "--verdict",
                "confirm",
                "--note",
                "P-R 합의 (prj orchestrator)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if rev.returncode != 0:
            print(f"[warn] handoff review 실패: {(rev.stderr or '').strip()}", file=sys.stderr)


def _resolve_source(raw: str | None) -> tuple[str | None, int, str]:
    """입력 경로와 크기를 정한다. --chunk 는 자동 판정된다.

    [WHY] 사람이 `--chunk`를 기억해야 하면 안 된다. 2026-09-29 실측 경계
    (55.8k자 통과 / 58.5k자 실패)를 기준으로 크기만 보고 스스로 판단한다.
    플래그가 없는 파일은 50,000자 초과 시 자동으로 map-reduce로 전환된다.

    반환: (경로, 글자수, 모드). 모드는 "argv" | "file" | "chunk".
    """
    if not raw:
        return None, 0, "argv"
    path = os.path.expanduser(raw)
    if os.path.isdir(path):
        # 디렉터리는 Obsidian 볼트 한 개로 간주한다 (PARA 구조).
        md = sorted(os.path.join(path, f) for f in os.listdir(path) if f.endswith(".md"))
        if not md:
            raise EngineError(f"no .md files in: {path}")
        path = md[0]
    if not os.path.isfile(path):
        raise EngineError(f"file not found: {path}")
    with open(path, encoding="utf-8", errors="replace") as fh:
        nchars = len(fh.read())
    mode = "chunk" if nchars > MAX_CONTEXT_CHARS else "file"
    return path, nchars, mode


def cmd_run(args: argparse.Namespace) -> int:
    task_id = args.task or f"prj-{uuid.uuid4().hex[:8]}"
    try:
        src, nchars, mode = _resolve_source(args.file)
    except EngineError as exc:
        print(f"[prj] {exc}", file=sys.stderr)
        return 1

    # [WHY] --no-chunk 는 사용자가 map-reduce를 명시적으로 끄는 경우에만 쓴다.
    chunk = mode == "chunk" and not args.no_chunk
    if mode == "chunk" and args.no_chunk:
        print(
            f"[prj] 문서가 {nchars:,}자로 상한({MAX_CONTEXT_CHARS:,})을 넘는데 "
            f"--no-chunk 로 강제했습니다. 브라우저 타이핑 한계(58.5k자)에서 실패할 수 있습니다.",
            file=sys.stderr,
        )
    if src:
        if chunk:
            eta = f"장문 {nchars:,}자 — 자동 map-reduce, 수 분 소요"
        else:
            eta = f"파일 {nchars:,}자 — 2~5분 소요"
    else:
        eta = "2~5분 소요"

    orch = PRJOrchestrator(
        proposer=WebLLMEngine("deepseek", think=True, file=src, chunk=chunk),
        reviewer=WebLLMEngine("qwen", think=False, file=src, chunk=chunk),
        adjudicator=DuckAIEngine() if not args.no_judge else None,
        max_rounds=args.max_rounds,
        on_log=lambda m: print(f"[prj] {m}", file=sys.stderr),
    )
    print(f"[prj] {task_id} 시작 — {eta}", file=sys.stderr)
    try:
        result = orch.run(task_id, args.query)
    except EngineError as exc:
        print(f"[prj] 실패: {exc}", file=sys.stderr)
        return 1
    _persist(result)
    _push_handoff(result)
    print(render_brief(result))
    if args.json:
        print(json.dumps(result.to_json(), ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    data = _load(args.task)
    print(
        json.dumps(
            {
                "task_id": data["task_id"],
                "outcome": data["outcome"],
                "phase": data["phase_reached"],
                "rounds": len(data["rounds"]),
                "elapsed_ms": data["elapsed_ms"],
                "sources": len((data.get("claim") or {}).get("sources", [])),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    from lib.prj.orchestrator import Outcome, Phase, Round

    data = _load(args.task)
    result = PRJResult(
        task_id=data["task_id"],
        question=data["question"],
        outcome=Outcome(data["outcome"]),
        phase_reached=Phase(data["phase_reached"]),
        rounds=[Round(**r) for r in data["rounds"]],
        adjudication=data.get("adjudication"),
        elapsed_ms=data.get("elapsed_ms", 0),
    )
    if data.get("claim"):
        from lib.prj.verify import Claim, VerifyResult

        result.claim = Claim(
            question=data["claim"]["question"],
            answer=data["claim"]["answer"],
            sources=data["claim"]["sources"],
        )
        v = data.get("verify") or {}
        result.verify = VerifyResult(
            ok=v.get("ok", False),
            sources=v.get("sources", []),
            errors=v.get("errors", []),
            warnings=v.get("warnings", []),
        )
    print(render_brief(result))
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    """사람이 1차 출처를 확인한 뒤에만 부른다. 이것이 유일한 승격 경로다."""
    proc = subprocess.run(  # noqa: S603
        [
            HANDOFF,
            "adjudicate",
            "--task",
            args.task,
            "--verdict",
            args.verdict,
            "--by",
            args.by,
            "--note",
            args.note or "",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if proc.returncode != 0:
        return proc.returncode
    if args.promote:
        promote = subprocess.run(  # noqa: S603
            [HANDOFF, "promote", "--task", args.task],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        sys.stdout.write(promote.stdout)
        sys.stderr.write(promote.stderr)
        return int(promote.returncode)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prj", description="웹 LLM P-R-J 교차검증 (HCP-MAD)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="P-R 실행 → 합의 시 즉시 종료")
    r.add_argument("query")
    r.add_argument("--task", default=None, help="task_id 지정 (기본: 자동 생성)")
    r.add_argument("--file", default=None, help="문서 경로 또는 Obsidian 디렉터리 (선택)")
    r.add_argument(
        "--no-chunk",
        action="store_true",
        help="자동 map-reduce를 끈다 (대용량에서 실패할 수 있다)",
    )
    r.add_argument("--max-rounds", type=int, default=3)
    r.add_argument("--no-judge", action="store_true", help="ECT(판정) 단계를 건너뛴다")
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("status")
    s.add_argument("task")
    s.set_defaults(func=cmd_status)

    b = sub.add_parser("brief")
    b.add_argument("task")
    b.set_defaults(func=cmd_brief)

    d = sub.add_parser("decide", help="1차 출처 확인 후 사람이 판정한다")
    d.add_argument("task")
    d.add_argument("--verdict", required=True, choices=["confirmed", "refuted", "partial"])
    d.add_argument("--by", default="user", help="판정 주체 (LLM 엔진명은 거부된다)")
    d.add_argument("--note", default="")
    d.add_argument("--promote", action="store_true", help="confirmed일 때 신뢰 저장소로")
    d.set_defaults(func=cmd_decide)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except EngineError as exc:
        print(f"[prj] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
