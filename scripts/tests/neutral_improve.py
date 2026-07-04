#!/usr/bin/env python3
# Status: experimental
# Path: none — NEUTRAL class improvement test (NextCoder 14B Q6)
"""NEUTRAL NLI Class Improvement — OLD vs V2 vs V3 (CoT + plausible unstated few-shot).

3 prompts on 3 NEUTRAL cases. Compares:
  OLD: E-C-N order, no few-shot
  V2:  N-C-E order + few-shot (different aspect, related inference)
  V3:  CoT reasoning field in JSON + all 3 NEUTRAL subtypes including plausible unstated
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.llm_client import call_llm
from lib.llm.json_parser import parse_llm_json
from lib.test_common import test_setup, test_heartbeat, test_complete
from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference

MODE_FILE = "/opt/ai_data/scripts/current-mode-inference.env"

NEUTRAL_CASES = [
    ("비타민C는 면역력에 좋다", "비타민C는 피부 미백에 도움된다", "NEUTRAL", "different aspect"),
    ("시스템은 PostgreSQL 16을 사용한다", "PostgreSQL 16은 빠르다", "NEUTRAL", "related inference"),
    ("ARM 서버에서 동작한다", "ARM Neoverse-N1 4코어", "NEUTRAL", "plausible unstated"),
]

OLD_SYSTEM = (
    "You are a code review verifier. Examine the finding below and decide:\n"
    "- ENTAILMENT: evidence is directly supported by the source text\n"
    "- CONTRADICTION: evidence contradicts the source text\n"
    "- NEUTRAL: evidence is related but not directly entailed\n\n"
    'Output STRICT JSON:\n'
    '{\n'
    '  "verification_items": [\n'
    '    {"check": "faithfulness", "result": "pass|fail|partial",\n'
    '     "grounding": "ENTAILMENT|CONTRADICTION|NEUTRAL"}\n'
    '  ]\n'
    '}'
)

V2_SYSTEM = (
    "You are a code review verifier. Examine the finding below and decide:\n"
    "- NEUTRAL: evidence is related but NOT directly entailed or contradicted\n"
    "  (e.g. same topic but different aspect, or plausible inference not in source)\n"
    "- CONTRADICTION: evidence contradicts the source text\n"
    "- ENTAILMENT: evidence is directly supported by the source text\n\n"
    "Example - NEUTRAL:\n"
    '  Source: "PostgreSQL 16을 데이터베이스로 사용한다"\n'
    '  Evidence: "PostgreSQL 16은 빠르다"\n'
    "  → NEUTRAL (속도는 source에 언급되지 않음)\n\n"
    "Example - NEUTRAL:\n"
    '  Source: "비타민C는 면역력에 좋다"\n'
    '  Evidence: "비타민C는 피부 미백에 도움된다"\n'
    "  → NEUTRAL (같은 주제지만 다른 측면)\n\n"
    'Output STRICT JSON:\n'
    '{\n'
    '  "verification_items": [\n'
    '    {"check": "faithfulness", "result": "pass|fail|partial",\n'
    '     "grounding": "NEUTRAL|CONTRADICTION|ENTAILMENT"}\n'
    '  ]\n'
    '}'
)

V3_COT_SYSTEM = (
    "You are a code review verifier. Determine the relationship between source text and evidence.\n\n"
    "Definitions:\n"
    "- ENTAILMENT: evidence is DIRECTLY STATED in the source — verbatim match, clear synonym, "
    "or direct paraphrase. Must be explicitly present, not just implied.\n"
    "- CONTRADICTION: evidence directly contradicts the source.\n"
    "- NEUTRAL: evidence is related but NOT directly stated. This includes plausible "
    "inferences, different aspects of the same topic, and related facts not mentioned.\n\n"
    "Examples:\n\n"
    'NEUTRAL (plausible unstated):\n'
    '  Source: "ARM 서버에서 동작한다"\n'
    '  Evidence: "ARM Neoverse-N1 4코어"\n'
    "  → Source says \"ARM server\" but never specifies which ARM processor. "
    "Neoverse-N1 is plausible but NOT directly stated. → NEUTRAL\n\n"
    'NEUTRAL (different aspect):\n'
    '  Source: "비타민C는 면역력에 좋다"\n'
    '  Evidence: "비타민C는 피부 미백에 도움된다"\n'
    "  → Both about Vitamin C but different benefit. Evidence is NOT in source. → NEUTRAL\n\n"
    'NEUTRAL (related inference):\n'
    '  Source: "시스템은 PostgreSQL 16을 사용한다"\n'
    '  Evidence: "PostgreSQL 16은 빠르다"\n'
    "  → Source names the database but doesn't discuss speed. → NEUTRAL\n\n"
    'ENTAILMENT (direct statement):\n'
    '  Source: "PostgreSQL 16을 데이터베이스로 사용한다"\n'
    '  Evidence: "PostgreSQL 16"\n'
    "  → Directly stated. → ENTAILMENT\n\n"
    'CONTRADICTION:\n'
    '  Source: "오늘 날씨가 좋다"\n'
    '  Evidence: "오늘 날씨가 나쁘다"\n'
    "  → Directly contradicts. → CONTRADICTION\n\n"
    "Analyze step by step:\n"
    "1. Is the evidence text DIRECTLY present (verbatim/synonym) in the source? → ENTAILMENT\n"
    "2. Does it DIRECTLY contradict the source? → CONTRADICTION\n"
    "3. Otherwise → NEUTRAL (plausible inference or different aspect = NEUTRAL)\n\n"
    'Output JSON with reasoning first, then the judgment:\n'
    '{\n'
    '  "reasoning": "Step-by-step analysis...",\n'
    '  "verification_items": [\n'
    '    {"check": "faithfulness", "result": "pass|fail|partial",\n'
    '     "grounding": "NEUTRAL|CONTRADICTION|ENTAILMENT"}\n'
    '  ]\n'
    '}'
)

PROMPTS = [
    ("OLD", OLD_SYSTEM),
    ("V2", V2_SYSTEM),
    ("V3_COT", V3_COT_SYSTEM),
]


def switch_inference():
    """Switch inference to NextCoder 14B Q6."""
    env = {
        "MODE": "test-q8", "MODEL_NAME": "test-nextcoder-q8",
        "PORT": "8083", "MODEL_FILE": "NextCoder-14B-q6_k_m.gguf",
        "CTX_SIZE": "8192", "THREADS": "4", "THREADS_BATCH": "4", "CACHE_RAM": "512",
    }
    lines = [f"{k}={v}" for k, v in env.items()]
    with open(MODE_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    _podman_stop_inference()
    _podman_start_inference()
    for i in range(120):
        h = subprocess.run(["curl", "-sf", "--max-time", "5", "http://127.0.0.1:8083/health"],
                           capture_output=True, text=True, timeout=10)
        if h.returncode == 0 and "ok" in h.stdout:
            print(f"  Health OK after {i*3}s", flush=True)
            return True
        time.sleep(3)
    return False


def run_case(system_prompt: str, source: str, evidence: str) -> dict:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f'Findings:\n  CTX-source: source text — "{source}"\n  EX-test: evidence — "{evidence}"'}
    ]
    try:
        meta = call_llm(messages, model="reviewer", max_tokens=512, temperature=0.0,
                        timeout=300, json_mode=True, return_meta=True)
        raw = meta.get("content", "")
        data = parse_llm_json(raw) if raw else None
        if data:
            items = data.get("verification_items", [])
            grounding = items[0].get("grounding", "N/A") if items else "N/A"
            reasoning = data.get("reasoning", "")
            return {"grounding": grounding, "reasoning": reasoning, "raw": raw[:200]}
        return {"grounding": "PARSE_FAIL", "reasoning": "", "raw": raw[:200]}
    except Exception as e:
        return {"grounding": "ERROR", "reasoning": str(e), "raw": ""}


def main():
    TEST = test_setup("neutral_improve", "NEUTRAL class — OLD vs V2 vs V3 CoT (NextCoder 14B Q6)")

    subprocess.run(["systemctl", "--user", "stop", "devforge-day-cycle.service"], capture_output=True, timeout=30)
    subprocess.run(["pkill", "-9", "-f", "day_cycle.sh"], capture_output=True, timeout=5)

    if not switch_inference():
        print("FATAL: inference not ready", flush=True)
        test_complete("error")
        return

    print("Warming up...", flush=True)
    call_llm([{"role": "user", "content": "Reply OK"}], model="reviewer", max_tokens=5, temperature=0.0, timeout=120)
    print("Warm-up OK", flush=True)

    results = {}

    for label, sys_prompt in PROMPTS:
        print(f"\n{'='*50}", flush=True)
        print(f"  [{label}] PROMPT — {len(NEUTRAL_CASES)} NEUTRAL cases", flush=True)
        print(f"{'='*50}", flush=True)
        test_heartbeat(f"{label} prompt")

        cases = []
        for i, (source, evidence, expected, desc) in enumerate(NEUTRAL_CASES, 1):
            r = run_case(sys_prompt, source, evidence)
            actual = r["grounding"]
            passed = actual == expected
            icon = "PASS" if passed else "FAIL"
            punch = f"  reasoning: {r['reasoning'][:100]}" if r["reasoning"] else ""
            print(f"  [{i}/3] {icon} {desc:25s} expected={expected:13s} actual={actual:13s}", flush=True)
            if r["reasoning"]:
                print(f"        {r['reasoning'][:120]}", flush=True)
            cases.append({"desc": desc, "expected": expected, "actual": actual, "passed": passed})
        results[label] = cases

    print(f"\n{'='*60}", flush=True)
    print(f"  COMPARISON: NEUTRAL accuracy by prompt", flush=True)
    print(f"{'='*60}", flush=True)
    for label, _ in PROMPTS:
        cases = results.get(label, [])
        ok = sum(1 for c in cases if c["passed"])
        n = len(cases)
        print(f"  {label:10s}: {ok}/{n} ({ok*100//n}%)", flush=True)

    print(f"\n{'='*60}", flush=True)
    print(f"  DETAIL", flush=True)
    print(f"{'='*60}", flush=True)
    for label, _ in PROMPTS:
        print(f"\n  --- {label} ---", flush=True)
        for c in results.get(label, []):
            icon = "PASS" if c["passed"] else "FAIL"
            print(f"    {icon} {c['desc']:25s} got={c['actual']:13s}", flush=True)

    test_complete("done")


if __name__ == "__main__":
    main()
