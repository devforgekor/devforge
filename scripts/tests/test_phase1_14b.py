#!/usr/bin/env python3
# Status: experimental
# Path: none — standalone test runner for Phase 1 (14B Q4 with/without Golden Set)
"""Phase 1: Test Qwen3-14B-Q4_K_M with/without Golden Set on the same full turn.

Devin-like self-healing test using TestRunner framework.
- Retry on error/FAIL (max 3, squared backoff)
- Health check between retries
- Structured result with quality metrics
- Observation recording to DB
- Cycle safe (stop/start via test_common)
- LSP integration for code analysis

Usage:
    python3 scripts/tests/test_phase1_14b.py
"""

import json
import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from lib.test_common import log, test_complete, test_heartbeat, test_setup
from lib.test_runner import PodBClient, RetryConfig, TestResult, print_comparison

# ── Config ────────────────────────────────────────────────────────────────

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "..", "docs", "predicate-golden-set.yaml")
TURN = """아닙니다, 현재 parallel=1이라 한 번에 1턴씩 순차 처리 중입니다. 코드를 보면 ThreadPoolExecutor(max_workers=PARALLEL)로 20개를 한꺼번에 submit 하지만, PARALLEL=1이면 1개씩만 실행됩니다.

**문제 재분석**: 이전 crash는 threads=4 (3코어 oversubscription)가 원인이었지 parallel=2가 원인이 아니었을 가능성이 있습니다. `threads=3 + parallel=2 + cpus=0-2`면 각 slot이 3코어를 공유하지만 oversubscription 없이 llama-server `--cont-batching`의 동적 배치를 활용할 수 있습니다.
Now update the pipeline scripts back to `parallel=2` to match.
Restart inference with threads=3 + parallel=2, re-run.
아직 env 파일이 parallel=1 상태. `ensure_model`으로 env 업데이트 후 재시작.
이제 `--parallel 2 --threads 3 --cpus 0-2` 적용 완료. 원래 crash 원인은 **threads=4가 3코어를 oversubscription**해서 ggml thread pool이 context switching 폭증 → "Remote end closed"였습니다. `threads=3`으로 코어 수에 맞추면 parallel=2도 안정적으로 동작합니다.

다시 20턴 실행:"""

SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "extraction",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "extractions": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "evidence": {"type": "string", "maxLength": 120},
                            "subject": {"type": "string", "maxLength": 30},
                            "predicate": {"type": "string", "maxLength": 40},
                            "object": {"type": "string", "maxLength": 60},
                        },
                        "required": ["evidence", "subject", "predicate", "object"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["extractions"],
            "additionalProperties": False,
        },
    },
}

BASE_PROMPT = """You are a fact extractor for a developer conversation. Extract factual triples (subject, predicate, object) that are EXPLICITLY present in the text.

Do NOT infer, summarize, or add information not present in the source.

RULES:
1. ATOMIC CLAIM: Each evidence MUST contain exactly ONE atomic claim.
2. SELF-CONTAINED: Resolve pronouns and implicit references.
3. SUBJECT-PREDICATE-OBJECT: Every fact MUST have all three. The predicate is a snake_case verb phrase describing the relation (NOT is/has/does/was/equals/exists).
4. CONCISE: Keep evidence under 12 words. Short, direct sentences only.
5. FAITHFULNESS: Directly traceable to source text. NO inference.
6. NO INVENT: If nothing extractable, return empty array. Do NOT force extraction."""


def load_golden_set(path: str):
    """Load golden set YAML, return (good_examples: list, bad_examples: list)."""
    with open(path) as f:
        gs = yaml.safe_load(f)
    good = [e for e in gs["entries"] if e["verdict"] == "GOOD"]
    bad = [e for e in gs["entries"] if e["verdict"] == "BAD"]
    return good, bad


def build_with_gs_prompt(good: list, bad: list) -> str:
    """Build system prompt with Golden Set examples."""
    lines = [BASE_PROMPT]
    lines.append("\n=== GOOD EXAMPLES (follow this style) ===")
    for e in good:
        lines.append(f"  evidence: {e['evidence']}")
        lines.append(f"  predicate: {e['predicate']}")
        lines.append(f"  subject: {e['subject']}  object: {e['object']}")
        lines.append("")
    lines.append("\n=== BAD EXAMPLES (DO NOT follow this style) ===")
    for e in bad:
        lines.append(f"  evidence: {e['evidence']}")
        lines.append(f"  ❌ BAD predicate: {e['predicate']}")
        if "reason" in e:
            lines.append(f"  reason: {e['reason']}")
        lines.append("")
    return "\n".join(lines)


def write_observation(result: TestResult, use_gs: bool):
    """Write structured observation to stderr for the hook to capture."""
    obs = json.dumps(
        {
            "domain": "extraction-test",
            "label": result.label,
            "use_gs": use_gs,
            "status": result.status,
            "facts": result.num_facts,
            "elapsed": round(result.elapsed, 1),
            "tokens": result.total_tokens,
            "error": result.error,
        },
        ensure_ascii=False,
    )
    # Hook captures this; also embedded in log for human reading
    log(f"[OBS] {obs}")


def main():
    # ── Setup: register heartbeat + stop day-cycle ──
    _TEST = test_setup("phase1_14b", "14B Q4 Golden Set comparison")
    log(f"Test turn chars: {len(TURN)}")
    log(f"Golden Set path: {GOLDEN_PATH}")

    # ── Init client ──
    client = PodBClient(container="devforge-inference", internal_port=8081, timeout=600)

    # ── Health check ──
    log("Health check...")
    h = client.health_check(timeout=15)
    if not h.ok:
        log(f"FAILED: {h.message}")
        test_complete("FAILED — health check")
        sys.exit(1)
    log(f"Health OK ({h.latency:.1f}s)")

    # ── Load golden set ──
    good, bad = load_golden_set(GOLDEN_PATH)
    log(f"Golden Set: {len(good)} GOOD, {len(bad)} BAD examples")
    prompt_no_gs = BASE_PROMPT
    prompt_with_gs = build_with_gs_prompt(good, bad)

    # ── Test A: WITHOUT Golden Set ──
    test_heartbeat("Test A (no GS) starting")
    log("\n" + "=" * 50)
    log("Test A: 14B Q4 WITHOUT Golden Set")
    log("=" * 50)

    result_a = client.retry(
        client.call_llm,
        [
            {"role": "system", "content": prompt_no_gs},
            {"role": "user", "content": TURN},
        ],
        schema=SCHEMA,
        max_tokens=1500,
        temperature=0.0,
        label="14B-no-gs",
        retry=RetryConfig(max_retries=3, backoff=15),
    )

    write_observation(result_a, use_gs=False)
    log(result_a.summary())
    if not result_a.error:
        for line in result_a.facts_table():
            log(line)

    # ── Test B: WITH Golden Set ──
    test_heartbeat("Test B (with GS) starting")
    log("\n" + "=" * 50)
    log("Test B: 14B Q4 WITH Golden Set")
    log("=" * 50)

    result_b = client.retry(
        client.call_llm,
        [
            {"role": "system", "content": prompt_with_gs},
            {"role": "user", "content": TURN},
        ],
        schema=SCHEMA,
        max_tokens=1500,
        temperature=0.0,
        label="14B-with-gs",
        retry=RetryConfig(max_retries=3, backoff=15),
    )

    write_observation(result_b, use_gs=True)
    log(result_b.summary())
    if not result_b.error:
        for line in result_b.facts_table():
            log(line)

    # ── Comparison Report ──
    test_heartbeat("Analyzing results")
    print_comparison(result_a, result_b, title="PHASE 1: 14B Q4 GS COMPARISON")

    # ── Verdict ──
    verdict = "PASS"
    if result_a.error and result_b.error or result_a.status == "FAIL" and result_b.status == "FAIL":
        verdict = "FAIL"
    elif result_b.is_good and not result_a.is_good:
        verdict = "PASS (GS improves quality)"
    elif result_b.is_good and result_a.is_good:
        verdict = "PASS (both GOOD, expected gap not visible)"
    elif result_b.is_usable and not result_a.is_usable:
        verdict = "PASS (GS marginally improves)"
    else:
        verdict = "INCONCLUSIVE"

    log(f"\n{'=' * 60}")
    log(f"VERDICT: {verdict}")
    log(f"{'=' * 60}")

    # ── Cleanup ──
    test_complete(f"Phase 1 complete — {verdict}")

    # Exit code for scripting
    if verdict.startswith("PASS"):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
