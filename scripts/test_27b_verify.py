#!/usr/bin/env python3
"""test_27b_verify.py — 27B verify 테스트 (기존 P-R-J handoff 데이터 사용)

27B가 기존 P-R-J 결과를 올바르게 verify하는지 검증.
SYS_V27 프롬프트로 handoff 문서 3개 전달 → final_verdict 산출 확인.

사용법:
  python3 test_27b_verify.py
"""

import json, sys, time, os

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)
from lib.llm_client import call_llm
from lib.llm.json_parser import parse_llm_json

# SYS_V27 (from prj_cycle.py line 594)
SYS_V27 = """You are a final verifier. Review all findings and P-R-J results.

You will receive THREE handoff documents:
1. [LLM-J] — Judge LLM handoff
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-J vs Python. After your final verdict,
write detailed, actionable feedback per model+role:
e.g., P=Qwen30B, R=Qwen14B, J=Selene — separate feedback for each.

=== EVALUATION RUBRIC (self-assessment) ===
Rate your OWN verification on these criteria:
- Thoroughness (0-10): Are all handoff documents compared and cross-checked?
- Evidence Check (0-10): Are verification items backed by specific data?
- Feedback Quality (0-10): Is per-role feedback actionable and constructive?

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "rubric_evaluation": {
    "thoroughness": "0-10",
    "thoroughness_justification": "...",
    "evidence_check": "0-10",
    "evidence_check_justification": "...",
    "feedback_quality": "0-10",
    "feedback_quality_justification": "..."
  },
  "feedback": {
    "P_Qwen30B": {"model":"Qwen30B","role":"proposer","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "R_Qwen14B": {"model":"Qwen14B","role":"reflector","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
    "J_Selene": {"model":"Selene","role":"judge","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
  },
  "handoff_comparison": {
    "better_handoff": "llm_j|python|equal",
    "reason": "why one handoff source was more useful for verification",
    "llm_j_strengths": ["..."],
    "python_strengths": ["..."]
  }
}"""


def main():
    print("=" * 60, flush=True)
    print("27B VERIFY TEST (P-R-J 기존 데이터)", flush=True)
    print("=" * 60, flush=True)

    # 1. Load experiment data
    exper_dir = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
    j_path = os.path.join(exper_dir, "exp_j_rot1_r1_norubric_r1_norubric.json")
    with open(j_path) as f:
        j_data = json.load(f)

    j_res = j_data.get("result", {})
    j_handoff = j_res.get("handoff", {})
    j_report = j_res.get("report", {})

    # Build 3 handoff documents (same as prj_cycle.py run_round)
    llm_text = "## Handoff (J=SeleneMini Q8) [LLM-J]\n" + json.dumps(
        {"handoff": j_handoff, "report": j_report}, ensure_ascii=False, indent=2)

    py_single = {
        "source": "python_compiled",
        "round": 1,
        "P_score": j_res.get("P_score", 0),
        "R_score": j_res.get("R_score", 0),
        "consensus": j_res.get("consensus_score", 0),
        "decision": j_res.get("decision", ""),
        "approved": j_res.get("approved", []),
        "rejected": j_res.get("rejected", []),
        "approved_count": len(j_res.get("approved", [])),
        "rejected_count": len(j_res.get("rejected", [])),
    }
    py_text = "## Handoff [Python]\n" + json.dumps(py_single, ensure_ascii=False, indent=2)
    pyc_text = json.dumps(py_single, ensure_ascii=False, indent=2)

    verifier_input = (
        "Below are 3 handoff documents:\n"
        "- 1 LLM-J\n"
        "- 1 Python\n"
        "- 1 Python consolidated\n\n"
        + f"\n\n---\n\n{llm_text}\n\n---\n\n{py_text}\n\n---\n\n"
        + f"## Consolidated Handoff [Python]\n{pyc_text}\n\n"
        "Compare LLM-J vs Python. "
        "In your 'handoff_comparison' field, state which source "
        "(llm_j or python) was more useful for verification overall and why."
    )

    print(f"\nLLM-J handoff: {json.dumps(j_handoff, ensure_ascii=False)[:200]}...", flush=True)
    print(f"Python handoff: approved={py_single['approved_count']}, rejected={py_single['rejected_count']}", flush=True)
    print(f"Verifier input: ~{len(verifier_input)} chars", flush=True)

    # 2. Call 27B verify
    print(f"\n[{time.strftime('%H:%M:%S')}] Calling 27B (verify mode, :8081)...", flush=True)
    t0 = time.monotonic()

    messages = [
        {"role": "system", "content": SYS_V27},
        {"role": "user", "content": verifier_input},
    ]

    resp = call_llm(messages, model="Qwen27B",
                    max_tokens=1024, timeout=1800,
                    json_mode=True, return_meta=True)

    elapsed = time.monotonic() - t0

    content = resp["content"] if isinstance(resp, dict) else resp
    result = parse_llm_json(content)

    usage = resp.get("usage", {})
    timings = resp.get("timings", {})

    # 3. Print results
    print(f"\n{'='*60}", flush=True)
    print(f"27B VERIFY RESULT ({elapsed:.0f}s)", flush=True)
    print(f"{'='*60}", flush=True)
    print(f"Tokens: {usage.get('prompt_tokens','?')}p + {usage.get('completion_tokens','?')}c", flush=True)
    if timings:
        ps = timings.get("prompt_per_second", 0)
        gs = timings.get("predicted_per_second", 0)
        print(f"Speed: prompt={ps:.1f} t/s, gen={gs:.1f} t/s", flush=True)

    if result:
        verdict = result.get("final_verdict", "?")
        action = result.get("action", "?")
        conf = result.get("confidence", "?")
        summary = result.get("summary", "")
        reasoning = result.get("reasoning", "")
        items = result.get("verification_items", [])
        hc = result.get("handoff_comparison", {})

        print(f"\n  Verdict: {verdict} (conf={conf})", flush=True)
        print(f"  Action: {action}", flush=True)
        print(f"  Summary: {summary}", flush=True)
        print(f"  Reasoning: {reasoning[:300]}", flush=True)
        print(f"\n  Verification items ({len(items)}):", flush=True)
        for item in items:
            print(f"    [{item.get('result','?')}] {item.get('check','')}", flush=True)

        print(f"\n  Handoff comparison: {hc.get('better_handoff','?')}", flush=True)
        print(f"    Reason: {hc.get('reason','')[:200]}", flush=True)

        fb = result.get("feedback", {})
        print(f"\n  Feedback:", flush=True)
        for role_key, role_fb in fb.items():
            print(f"    {role_key}: score={role_fb.get('score','?')}", flush=True)
            for s in role_fb.get("strengths", []):
                print(f"      + {s}", flush=True)
    else:
        print(f"\n  Raw content (first 500): {content[:500]}", flush=True)

    # 4. Save result
    out_path = os.path.join(exper_dir, "exp_27b_verify_test.json")
    with open(out_path, "w") as f:
        json.dump({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "elapsed_s": round(elapsed),
            "usage": usage,
            "timings": timings,
            "result": result,
            "raw_content": content[:500],
        }, f, ensure_ascii=False, indent=2)
    print(f"\nSaved: {out_path}", flush=True)
    print("DONE.", flush=True)


if __name__ == "__main__":
    main()
