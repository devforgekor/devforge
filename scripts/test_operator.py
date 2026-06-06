#!/usr/bin/env python3
"""Operator model evaluation — mode dispatch for multi-LLM pipeline.

The Operator is a lightweight model that decides WHICH DevForge mode
to invoke. It does NOT write code or review — it dispatches to the
appropriate specialist model(s).

Modes:
  generate    → 30B standalone code generation     (Pod A only)
  debate      → 30B Draft + 3B Reviewer debate      (Pod A + Pod B:3B)
  review      → 30B Draft + 14B/R1-8B Review        (Pod A + Pod B:review)
  cooperative → Azure spot VMs multi-agent           (remote)
  verify      → 32B standalone final verification    (Pod B:32B only)
  general     → quick Q&A, no specialist needed      (any lightweight)

Usage:
  python3 scripts/test_operator.py              # both models
  python3 scripts/test_operator.py --model qwen # Qwen3-1.7B only
  python3 scripts/test_operator.py --model llama # Llama-3.2-3B only
  python3 scripts/test_operator.py --quick       # 8-test smoke check
"""

import json
import os
import re
import time
import urllib.request
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LLM_ENDPOINT = os.getenv("OPERATOR_ENDPOINT", "http://127.0.0.1:8081/v1/chat/completions")

MODELS = {
    "qwen": {
        "name": "qwen3-1.7b",
        "file": "Qwen3-1.7B-Q4_K_M.gguf",
        "family": "Qwen3",
        "size_gb": 1.1,
    },
    "llama": {
        "name": "llama-3.2-3b",
        "file": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "family": "Llama-3.2",
        "size_gb": 2.0,
    },
}

SYSTEM_PROMPT = """[ROLE]
You are the DevForge pipeline operator — a mode dispatcher.
You decide WHICH workflow to run. You do NOT write, review, or modify code.

[AVAILABLE MODES]
- "generate"   : simple code generation → Qwen3-30B-MoE standalone
- "debate"     : multi-perspective debate → 30B Draft + 3B Reviewer
- "review"     : systematic code review → 30B Draft + 14B/R1-8B Reviewer
- "cooperative": complex multi-agent task → Azure spot VMs (remote)
- "verify"     : final quality gate check → 32B standalone verification
- "general"    : quick Q&A, no specialist needed → lightweight model

[DISPATCH RULES]
1. If the user wants NEW CODE written or a feature IMPLEMENTED → "generate"
2. If the user wants code REVIEWED, DEBUGGED, or analyzed for bugs → "review"
3. If the user presents a DESIGN DECISION or ARCHITECTURAL CHOICE
   with multiple valid approaches → "debate"
4. If the task requires MULTIPLE agents, LONG-RUNNING orchestration,
   or EXTERNAL resources → "cooperative"
5. If the user wants FINAL SIGN-OFF, pre-merge VERIFICATION,
   or quality GATE CHECK → "verify"
6. If the user asks a GENERAL question (no code, no design) → "general"

[OUTPUT FORMAT]
Respond with ONLY a JSON object:
{"mode": "<mode_name>", "complexity": "low|medium|high|extreme", "reason": "<one sentence>"}

[CONSTRAINTS]
1. Valid JSON only. No prose, no markdown fences, no explanation.
2. Default uncertain inputs to "general".
3. Reason under 20 words.
4. complexity guides resource allocation: low=any model, high=need full pipeline."""

# ---------------------------------------------------------------------------
# Test cases — (prompt, expected_mode, expected_complexity, description)
# ---------------------------------------------------------------------------
FULL_TESTS = [
    # === generate — code creation ===
    ("Write a Python function to sort a list of dictionaries by a key",
     "generate", "low", "simple function"),
    ("Implement a REST API endpoint for user login in FastAPI",
     "generate", "medium", "API endpoint"),
    ("Add a retry mechanism with exponential backoff to the HTTP client",
     "generate", "medium", "feature addition"),
    ("Create a React component that displays a paginated table",
     "generate", "medium", "frontend component"),
    ("Write a bash script to rotate logs older than 7 days",
     "generate", "low", "simple script"),
    ("Generate a SQL migration to add an email column",
     "generate", "low", "DB migration"),

    # === review — code analysis ===
    ("Review this code for security vulnerabilities",
     "review", "medium", "security audit"),
    ("Find bugs in the following Python function",
     "review", "medium", "bug hunting"),
    ("Is there a memory leak in this C++ code?",
     "review", "medium", "memory analysis"),
    ("How can I improve the performance of this database query?",
     "review", "medium", "query optimization"),
    ("Check this code for race conditions in concurrent access",
     "review", "high", "concurrency audit"),
    ("What's wrong with my error handling here?",
     "review", "low", "error handling check"),
    ("Is this regex safe from ReDoS attacks?",
     "review", "medium", "regex security"),
    ("Audit this authentication middleware for OWASP top 10 issues",
     "review", "high", "OWASP audit"),

    # === debate — design/architecture decisions ===
    ("Should we use Redis or Kafka for our message queue?",
     "debate", "medium", "tech choice"),
    ("Microservices vs monolith for a 5-person startup — which is better?",
     "debate", "high", "architectural decision"),
    ("Is it better to use JWT or session-based auth for this API?",
     "debate", "medium", "auth strategy"),
    ("Should I refactor this 2000-line class or rewrite it from scratch?",
     "debate", "high", "refactor vs rewrite"),
    ("What's the best way to handle distributed transactions across microservices?",
     "debate", "high", "distributed design"),
    ("React Server Components vs traditional SSR — which fits our use case?",
     "debate", "medium", "frontend architecture"),

    # === cooperative — complex multi-agent tasks ===
    ("Analyze our entire codebase for tech debt and create a remediation plan",
     "cooperative", "extreme", "full codebase analysis"),
    ("Set up a CI/CD pipeline with automated testing and deployment to 3 environments",
     "cooperative", "high", "multi-env CI/CD"),
    ("Migrate our PostgreSQL schema from v2 to v3 with zero downtime",
     "cooperative", "high", "zero-downtime migration"),
    ("Build a complete user management system: auth, roles, permissions, audit log",
     "cooperative", "high", "multi-module system"),
    ("Run penetration testing on our API and generate a compliance report",
     "cooperative", "extreme", "security + compliance"),

    # === verify — final quality gate ===
    ("Is this code ready for production deployment?",
     "verify", "medium", "deploy readiness"),
    ("Run the final verification checklist before merging to main",
     "verify", "medium", "pre-merge gate"),
    ("Validate that all error paths are handled before the release",
     "verify", "high", "release validation"),
    ("Check if this PR meets our coding standards and test coverage requirements",
     "verify", "medium", "PR standards check"),
    ("Final sign-off: does the v2.0 release candidate pass all quality gates?",
     "verify", "high", "release sign-off"),

    # === general — no code involved ===
    ("What is the difference between TCP and UDP?",
     "general", "low", "networking concept"),
    ("Explain how garbage collection works in Python",
     "general", "low", "language internals"),
    ("How do I set up a cron job in Linux?",
     "general", "low", "sysadmin question"),
    ("What's the weather like today?",
     "general", "low", "non-tech"),
    ("Summarize the key changes in Python 3.12",
     "general", "low", "changelog"),

    # === edge cases ===
    ("I need a login page. Can you also check if it's secure?",
     "generate", "medium", "primary intent: create"),
    ("How should I structure my project directory?",
     "debate", "low", "design decision"),
    ("Debug why my API returns 500 errors in production",
     "review", "high", "production debugging"),
    ("help",
     "general", "low", "ambiguous single word"),
    ("",
     "general", "low", "empty prompt"),
    ("   ",
     "general", "low", "whitespace only"),
    ("Fix the indentation in this file and then explain what was wrong",
     "review", "low", "fix + explain = review"),
    ("Write tests for this module and run coverage",
     "generate", "medium", "test creation = generate"),

    # === Korean prompts ===
    ("정렬 알고리즘을 파이썬으로 작성해줘",
     "generate", "low", "한국어 코드 생성"),
    ("이 코드에서 보안 취약점을 찾아줘",
     "review", "medium", "한국어 보안 리뷰"),
    ("마이크로서비스랑 모놀리스 중에 우리 팀에 뭐가 더 나을까?",
     "debate", "medium", "한국어 아키텍처 결정"),
    ("CI/CD 파이프라인 구축해줘",
     "cooperative", "high", "한국어 multi-step"),
    ("프로덕션 배포 전에 최종 검증해줘",
     "verify", "medium", "한국어 deploy gate"),
    ("도커가 뭔지 설명해줘",
     "general", "low", "한국어 일반 질문"),
]

# Quick smoke test (one per mode + edge)
QUICK_TESTS = [
    ("Write a Python function to sort a list", "generate", "low", "gen"),
    ("Review this code for bugs", "review", "medium", "review"),
    ("Should I use NoSQL or SQL for this project?", "debate", "medium", "design"),
    ("Build a complete authentication system with OAuth", "cooperative", "high", "multi"),
    ("Is this PR ready to merge?", "verify", "medium", "gate"),
    ("What is Python?", "general", "low", "qa"),
    ("help", "general", "low", "ambiguous"),
    ("이 배포 파이프라인 최종 점검해줘", "verify", "medium", "한국어 verify"),
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)

def extract_json(raw: str) -> Optional[dict]:
    for method in ["direct", "fence", "regex"]:
        if method == "direct":
            try:
                return json.loads(raw.strip())
            except json.JSONDecodeError:
                pass
        elif method == "fence":
            for marker in ("```json", "```"):
                if marker in raw:
                    start = raw.find(marker) + len(marker)
                    end = raw.find("```", start)
                    if end > start:
                        try:
                            return json.loads(raw[start:end].strip())
                        except json.JSONDecodeError:
                            pass
        elif method == "regex":
            for m in re.finditer(r"\{[^{}]*\}", raw):
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    continue
    return None

def call_llm(model_name: str, prompt: str, timeout: int = 60) -> Tuple[int, dict]:
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 150,
    }
    if model_name:
        body["model"] = model_name

    data = json.dumps(body).encode()
    req = urllib.request.Request(LLM_ENDPOINT, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
        content = result["choices"][0]["message"]["content"]
        parsed = extract_json(content)
        usage = result.get("usage", {})
        return 200, {
            "raw": content,
            "parsed": parsed,
            "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_completion": usage.get("completion_tokens", 0),
        }
    except Exception as e:
        return 0, {"error": str(e)[:200]}

def score_complexity(got: Optional[str], expected: str) -> bool:
    """Complexity is directional — 'extreme' for 'high' is close enough."""
    if got == expected:
        return True
    order = {"low": 0, "medium": 1, "high": 2, "extreme": 3}
    if got in order and expected in order:
        return abs(order[got] - order[expected]) <= 1
    return False

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_tests(model_key: str, tests: list, label: str) -> dict:
    cfg = MODELS[model_key]
    model_name = cfg["name"]
    log(f"\n{'='*60}")
    log(f"Testing: {cfg['family']} ({model_name}) — {label}")
    log(f"File: {cfg['file']} | Size: {cfg['size_gb']}GB")
    log(f"{'='*60}")

    results = {
        "total": 0, "mode_correct": 0, "complexity_ok": 0,
        "json_valid": 0, "errors": [], "latencies": [],
        "per_mode": {},
    }

    for prompt, exp_mode, exp_complexity, desc in tests:
        results["total"] += 1
        t0 = time.monotonic()
        status, body = call_llm(model_name, prompt)
        elapsed = time.monotonic() - t0
        results["latencies"].append(elapsed)

        mode = None
        complexity = None
        if body.get("parsed"):
            results["json_valid"] += 1
            mode = body["parsed"].get("mode")
            complexity = body["parsed"].get("complexity")

        mode_ok = (mode == exp_mode)
        comp_ok = score_complexity(complexity, exp_complexity)
        if mode_ok:
            results["mode_correct"] += 1
        if comp_ok:
            results["complexity_ok"] += 1

        # Per-mode stats
        if exp_mode not in results["per_mode"]:
            results["per_mode"][exp_mode] = {"total": 0, "correct": 0}
        results["per_mode"][exp_mode]["total"] += 1
        if mode_ok:
            results["per_mode"][exp_mode]["correct"] += 1

        # Display
        markers = []
        if not mode_ok:
            markers.append(f"mode:{exp_mode}!={mode}")
        if not comp_ok and mode_ok:
            markers.append(f"comp:{exp_complexity}!={complexity}")
        status_str = "PASS" if mode_ok else "FAIL"
        detail = " ".join(markers) if markers else ""

        if not mode_ok:
            results["errors"].append({
                "desc": desc, "prompt": prompt[:80],
                "exp_mode": exp_mode, "got_mode": mode,
                "exp_comp": exp_complexity, "got_comp": complexity,
                "raw": body.get("raw", "")[:100],
            })

        mode_str = mode or "INVALID"
        comp_str = complexity or "-"
        j = "OK" if body.get("parsed") else "NOJSON"
        log(f"  [{status_str}] {elapsed:.2f}s j={j} "
            f"mode={mode_str:<13} comp={comp_str:<8} | {desc} {detail}")

    # Summary
    acc = results["mode_correct"] / results["total"] * 100 if results["total"] else 0
    jr = results["json_valid"] / results["total"] * 100 if results["total"] else 0
    cr = results["complexity_ok"] / results["total"] * 100 if results["total"] else 0
    avg_lat = (sum(results["latencies"]) / len(results["latencies"])
               if results["latencies"] else 0)
    lat_sorted = sorted(results["latencies"])

    log(f"\n--- {model_key.upper()} Summary ---")
    log(f"  Mode accuracy:    {results['mode_correct']}/{results['total']} ({acc:.0f}%)")
    log(f"  Complexity +/-1:  {results['complexity_ok']}/{results['total']} ({cr:.0f}%)")
    log(f"  JSON valid:       {results['json_valid']}/{results['total']} ({jr:.0f}%)")
    log(f"  Latency:          avg={avg_lat:.2f}s p50={lat_sorted[len(lat_sorted)//2]:.2f}s "
        f"min={lat_sorted[0]:.2f}s max={lat_sorted[-1]:.2f}s")
    for mode_name in ["generate", "review", "debate", "cooperative", "verify", "general"]:
        if mode_name in results["per_mode"]:
            m = results["per_mode"][mode_name]
            m_acc = m["correct"] / m["total"] * 100 if m["total"] else 0
            log(f"  {mode_name:13s}: {m['correct']}/{m['total']} ({m_acc:.0f}%)")

    if results["errors"]:
        log(f"\n  Mode errors ({len(results['errors'])}):")
        for e in results["errors"][:8]:
            log(f"    [{e['desc']}] exp={e['exp_mode']} got={e['got_mode']} "
                f"| {e['raw'][:60]}")

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    ap = argparse.ArgumentParser(description="DevForge Operator model evaluation")
    ap.add_argument("--model", choices=["qwen", "llama"],
                    help="Test single model only")
    ap.add_argument("--quick", action="store_true",
                    help="Run 8-test smoke check")
    args = ap.parse_args()

    test_set = QUICK_TESTS if args.quick else FULL_TESTS
    label = "smoke" if args.quick else "full"

    all_results = {}
    models_to_test = [args.model] if args.model else list(MODELS.keys())

    for mk in models_to_test:
        all_results[mk] = run_tests(mk, test_set, label)

    if len(models_to_test) == 2:
        log(f"\n{'='*60}")
        log("HEAD-TO-HEAD")
        log(f"{'='*60}")
        log(f"{'':14s} {'mode acc':>9s} {'json':>6s} {'p50 lat':>8s} {'RAM':>6s}")
        log(f"{'':14s} {'-'*9} {'-'*6} {'-'*8} {'-'*6}")
        for mk in models_to_test:
            r = all_results[mk]
            acc = r["mode_correct"] / r["total"] * 100 if r["total"] else 0
            jr = r["json_valid"] / r["total"] * 100 if r["total"] else 0
            lat = sorted(r["latencies"])
            p50 = lat[len(lat)//2] if lat else 0
            log(f"  {MODELS[mk]['family']:12s} {acc:8.0f}% {jr:5.0f}% {p50:7.2f}s "
                f"{MODELS[mk]['size_gb']:5.1f}GB")

        qwen_acc = (all_results["qwen"]["mode_correct"] / all_results["qwen"]["total"] * 100
                     if all_results["qwen"]["total"] else 0)
        llama_acc = (all_results["llama"]["mode_correct"] / all_results["llama"]["total"] * 100
                      if all_results["llama"]["total"] else 0)

        log(f"\n  Verdict: ", end="")
        if abs(qwen_acc - llama_acc) < 5:
            log(f"TOO CLOSE (Δ={abs(qwen_acc-llama_acc):.0f}%) → prefer Qwen3-1.7B (smaller)")
        elif qwen_acc > llama_acc:
            log(f"Qwen3-1.7B wins ({qwen_acc:.0f}% vs {llama_acc:.0f}%)")
        else:
            log(f"Llama-3.2-3B wins ({llama_acc:.0f}% vs {qwen_acc:.0f}%)")

    out_path = "/var/tmp/code_mod_tests/operator_test_results.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False, default=str)
    log(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
