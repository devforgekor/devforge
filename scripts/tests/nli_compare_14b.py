#!/usr/bin/env python3
# Status: experimental
# Path: none — 14B NLI grounding comparison (NextCoder vs Qwen2.5-Coder)
"""14B NLI Grounding Comparison — sequential test of both 14B Q8 models.

Follows test_common conventions: auto protection, heartbeat, cleanup.
Switches Pod B between NextCoder-14B-Q8 and Qwen2.5-Coder-14B-Q8,
runs 13 synthetic NLI test cases on each, saves results to data/eval/.

Usage:
  python3 scripts/tests/nli_compare_14b.py
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVAL_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "eval")
os.makedirs(EVAL_DIR, exist_ok=True)
sys.path.insert(0, SCRIPTS_DIR)

from lib.llm_client import call_llm
from lib.llm.json_parser import parse_llm_json
from lib.test_common import test_setup, test_heartbeat, test_complete

MODE_FILE = "/opt/ai_data/scripts/current-mode-pod-b.env"

# ── NLI Test Cases (13 known-truth pairs) ──

TEST_CASES = [
    # ENTAILMENT (5)
    ("한국의 수도는 서울이다", "서울은 대한민국의 수도", "ENTAILMENT", "same meaning Korean"),
    ("Python is a dynamically typed language", "Python uses dynamic typing", "ENTAILMENT", "same meaning English"),
    ("PostgreSQL 16을 데이터베이스로 사용한다", "PostgreSQL 16", "ENTAILMENT", "extract faithful"),
    ("철수는 학교에 갔다. 영희는 집에 있었다.", "철수는 학교에 갔다", "ENTAILMENT", "subset extraction"),
    ("안녕하세여 저는 개발자입 니다", "안녕하세요 저는 개발자입니다", "ENTAILMENT", "spelling fix"),
    # CONTRADICTION (5)
    ("오늘 날씨가 좋다", "오늘 날씨가 나쁘다", "CONTRADICTION", "negation Korean"),
    ("The sky is blue", "The sky is green", "CONTRADICTION", "negation English"),
    ("지구는 둥글다", "지구는 평평하다", "CONTRADICTION", "factual contradiction"),
    ("한국 인구는 약 5100만 명이다", "한국 인구는 1억 명이다", "CONTRADICTION", "wrong number"),
    ("시스템은 PostgreSQL을 사용한다", "시스템은 MongoDB를 사용한다", "CONTRADICTION", "wrong entity"),
    # NEUTRAL (3)
    ("시스템은 PostgreSQL 16을 사용한다", "PostgreSQL 16은 빠르다", "NEUTRAL", "related inference"),
    ("비타민C는 면역력에 좋다", "비타민C는 피부 미백에 도움된다", "NEUTRAL", "different aspect"),
    ("ARM 서버에서 동작한다", "ARM Neoverse-N1 4코어", "NEUTRAL", "plausible unstated"),
]


def _switch_pod_b(model_key: str) -> bool:
    """Write env file for test model and restart Pod B container.

    Direct approach (not via ensure_model) to avoid race with protection.
    """
    env_map = {
        "test_nextcoder_q8": {
            "MODE": "test-q8", "MODEL_NAME": "test-nextcoder-q8",
            "PORT": "8083", "MODEL_FILE": "NextCoder-14B-q6_k_m.gguf",
            "CTX_SIZE": "8192", "THREADS": "4", "THREADS_BATCH": "4",
            "CACHE_RAM": "512",
        },
        "test_qwen_14b": {
            "MODE": "test-q8", "MODEL_NAME": "test-qwen-14b",
            "PORT": "8083", "MODEL_FILE": "Qwen2.5-Coder-14B-Instruct-Q6_K.gguf",
            "CTX_SIZE": "8192", "THREADS": "4", "THREADS_BATCH": "4",
            "CACHE_RAM": "512",
        },
    }
    cfg = env_map.get(model_key)
    if not cfg:
        print(f"  [switch] Unknown model: {model_key}")
        return False

    lines = [f"{k}={v}" for k, v in cfg.items()]
    with open(MODE_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  [switch] Env written for {model_key}")

    # Restart container
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-pod-b.service"],
        capture_output=True, timeout=60
    )
    print(f"  [switch] Restart exit={r.returncode}")

    # Wait for port forwarding
    for i in range(20):
        r2 = subprocess.run(
            ["ss", "-tlnp"], capture_output=True, text=True, timeout=5
        )
        if "8083" in r2.stdout:
            print(f"  [switch] Port 8083 visible after {i*1}s")
            break
        time.sleep(1)
    else:
        print(f"  [switch] Port 8083 not visible after 20s — continuing anyway")

    # Wait for health
    for i in range(120):
        h = subprocess.run(
            ["curl", "-sf", "--max-time", "5", "http://127.0.0.1:8083/health"],
            capture_output=True, text=True, timeout=10
        )
        if h.returncode == 0 and "ok" in h.stdout:
            print(f"  [switch] Health OK after {i*3}s")
            return True
        time.sleep(3)
    print(f"  [switch] Health TIMEOUT after 360s")
    return False


def build_prompt(source: str, evidence: str) -> list:
    system = (
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
    user = (
        f'Findings:\n'
        f'  CTX-source: source text — "{source}"\n'
        f'  EX-test: evidence — "{evidence}"'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def run_case(messages: list) -> dict:
    """Single NLI case with 300s timeout for cold-start 14B."""
    t0 = time.monotonic()
    try:
        meta = call_llm(
            messages, model="reviewer",
            max_tokens=256, temperature=0.0,
            timeout=300, json_mode=True, return_meta=True
        )
        elapsed = time.monotonic() - t0
        raw = meta.get("content", "")
        # Strip markdown code fences from NextCoder response
        data = parse_llm_json(raw) if raw else None
        if data:
            items = data.get("verification_items", [])
            grounding = items[0].get("grounding", "N/A") if items else "N/A"
        else:
            grounding = "PARSE_FAIL"
        return {"ok": True, "grounding": grounding, "elapsed_s": round(elapsed, 1)}
    except Exception as e:
        return {"ok": False, "grounding": "ERROR", "elapsed_s": round(time.monotonic() - t0, 1)}


def test_model(model_key: str, model_label: str) -> dict:
    """Switch Pod B to model, run all 13 NLI cases."""
    print(f"\n{'='*60}", flush=True)
    print(f"  Switching → {model_label} ({model_key})", flush=True)
    print(f"{'='*60}", flush=True)
    test_heartbeat(f"switch {model_label}")

    if not _switch_pod_b(model_key):
        return {"model": model_label, "error": "pod_start_failed"}

    # Warm-up: one cheap inference to cover model cold-start
    print(f"  Warming up...", flush=True)
    try:
        call_llm(
            [{"role": "user", "content": "Reply with just OK"}],
            model="reviewer", max_tokens=5, temperature=0.0,
            timeout=300
        )
        print(f"  Warm-up OK", flush=True)
    except Exception as e:
        print(f"  Warm-up failed: {e}", flush=True)
    test_heartbeat(f"testing {model_label}")

    results = []
    correct = 0
    total = len(TEST_CASES)
    by_class = {"ENTAILMENT": {"ok": 0, "total": 0},
                "CONTRADICTION": {"ok": 0, "total": 0},
                "NEUTRAL": {"ok": 0, "total": 0}}

    for i, (source, evidence, expected, desc) in enumerate(TEST_CASES, 1):
        msgs = build_prompt(source, evidence)
        r = run_case(msgs)
        actual = r["grounding"]
        passed = actual == expected
        if passed:
            correct += 1
        if expected in by_class:
            by_class[expected]["total"] += 1
            if passed:
                by_class[expected]["ok"] += 1

        icon = "PASS" if passed else "FAIL"
        print(f"  [{i:2d}/{total}] {icon} {desc:25s}  expected={expected:13s}  actual={actual:13s}  ({r['elapsed_s']:4.1f}s)", flush=True)
        test_heartbeat(f"{model_label} {i}/{total}")

        results.append({
            "description": desc, "source": source, "evidence": evidence,
            "expected": expected, "actual": actual,
            "passed": passed, "elapsed_s": r["elapsed_s"],
        })

    acc = correct / total * 100
    avg_t = sum(r["elapsed_s"] for r in results) / total if results else 0

    output = {
        "model": model_label,
        "model_key": model_key,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "accuracy": round(acc, 1),
        "correct": correct, "total": total,
        "by_class": {k: {"ok": v["ok"], "total": v["total"],
                          "acc": round(v["ok"]/v["total"]*100, 1) if v["total"] > 0 else 0}
                     for k, v in sorted(by_class.items())},
        "avg_elapsed_s": round(avg_t, 1),
        "results": results,
    }

    fname = f"nli_compare_{model_label.replace(' ','_')}.json"
    fpath = os.path.join(EVAL_DIR, fname)
    with open(fpath, "w") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {fpath}", flush=True)
    return output


def print_comparison(all_results: list):
    """Side-by-side comparison."""
    print(f"\n{'='*60}", flush=True)
    print(f"  FINAL COMPARISON", flush=True)
    print(f"{'='*60}", flush=True)
    valid = [r for r in all_results if "error" not in r]
    if len(valid) < 2:
        print(f"  Not enough valid results to compare", flush=True)
        return

    m1, m2 = valid[0], valid[1]
    print(f"\n  {'':30s} {'NextCoder':>18s} {'Qwen2.5':>18s}")
    print(f"  {'─'*66}")
    print(f"  {'Accuracy':30s} {m1['accuracy']:>17.1f}% {m2['accuracy']:>17.1f}%")
    print(f"  {'Avg time':30s} {m1['avg_elapsed_s']:>17.1f}s {m2['avg_elapsed_s']:>17.1f}s")
    for cls in ("ENTAILMENT", "CONTRADICTION", "NEUTRAL"):
        a1 = m1["by_class"].get(cls, {})
        a2 = m2["by_class"].get(cls, {})
        s1 = f"{a1.get('ok',0)}/{a1.get('total',0)} ({a1.get('acc',0):.0f}%)"
        s2 = f"{a2.get('ok',0)}/{a2.get('total',0)} ({a2.get('acc',0):.0f}%)"
        print(f"  {f'{cls} accuracy':30s} {s1:>18s} {s2:>18s}", flush=True)

    print(f"\n  {'Test case':30s} {'NextCoder':>18s} {'Qwen2.5':>18s}")
    print(f"  {'─'*66}")
    for ti, tc in enumerate(TEST_CASES):
        desc = tc[3][:28]
        a1 = m1["results"][ti]["actual"] if ti < len(m1["results"]) else "?"
        a2 = m2["results"][ti]["actual"] if ti < len(m2["results"]) else "?"
        p1 = "PASS" if a1 == tc[2] else "FAIL"
        p2 = "PASS" if a2 == tc[2] else "FAIL"
        print(f"  {p1:4s} {desc:27s} {a1:>18s} {a2:>18s} {p2:4s}", flush=True)

    w1, w2 = valid[0]["model"], valid[1]["model"]
    winner = w1 if m1["accuracy"] > m2["accuracy"] else w2 if m2["accuracy"] > m1["accuracy"] else "TIE"
    print(f"\n  Winner: {winner}", flush=True)
    print(f"{'='*60}", flush=True)


def main():
    # 1) Register protection + heartbeat (auto cleanup on exit)
    TEST = test_setup("nli_compare_14b",
                      "NextCoder 14B Q8 vs Qwen2.5-Coder 14B Q8 NLI grounding comparison")

    # 2) Stop day_cycle if running (may conflict with Pod B restarts)
    subprocess.run(["systemctl", "--user", "stop", "devforge-day-cycle.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["pkill", "-9", "-f", "day_cycle.sh"], capture_output=True, timeout=5)
    print(f"  [init] Cycles stopped", flush=True)

    MODELS = [
        ("test_nextcoder_q8", "NextCoder-14B-Q8"),
        ("test_qwen_14b", "Qwen2.5-Coder-14B-Q8"),
    ]

    all_results = []
    for model_key, model_label in MODELS:
        result = test_model(model_key, model_label)
        all_results.append(result)

    print_comparison(all_results)

    # Save combined report
    combined = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_cases": len(TEST_CASES),
        "test_cases": TEST_CASES[:],
        "models": [{
            "label": r.get("model", "?"),
            "accuracy": r.get("accuracy"),
            "avg_elapsed_s": r.get("avg_elapsed_s"),
            "by_class": r.get("by_class"),
        } for r in all_results],
    }
    fpath = os.path.join(EVAL_DIR, "nli_compare_combined.json")
    with open(fpath, "w") as f:
        json.dump(combined, f, ensure_ascii=False, indent=2)
    print(f"\n  Combined report: {fpath}", flush=True)

    test_complete("done")


if __name__ == "__main__":
    main()
