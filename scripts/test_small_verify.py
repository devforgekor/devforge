#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""Qwen2.5-Coder-3B 30B verify 역할 테스트 — 동일 프롬프트로 3B vs 30B 비교."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.llm_client import call_llm

EXPER_DIR = "/opt/projects/server/data/experiment"
PIPELINE_STATE = os.path.join(EXPER_DIR, "pipeline_state_r1_norubric.json")
EXP_30B = os.path.join(EXPER_DIR, "exp_30b_r1_norubric_r1_norubric.json")
OUTPUT_3B = os.path.join(EXPER_DIR, "exp_3b_verify.json")

with open(PIPELINE_STATE) as f:
    state = json.load(f)

with open(EXP_30B) as f:
    exp_30b = json.load(f)

inp = state["input"]
findings = inp["findings"]
ext = state["extract"]
py_verify = state["python_verify"]
sev = inp["severity_distribution"]

# ── Build context = build_context("30b_verify") ──
parts = []
parts.append("=== PIPELINE CONTEXT: Round 1 ===\n")

# Extract summary
extract_models = ext.get("models", [])
if extract_models:
    parts.append(f"[3B EXTRACT] {len(extract_models)} models x 15 turns")
    for m in extract_models:
        fth = f"{m.get('faithfulness_rate',0)*100:.0f}%"
        sec = m.get('elapsed_seconds', 0)
        ext_count = m.get('total_extractions', 0)
        parts.append(f"  {m['model']}: faith={fth} ({m.get('total_faithful',0)}/{ext_count}), "
                     f"time={sec:.0f}s ({m.get('avg_time_per_turn','?')}s/turn), ok={m.get('turns_ok',0)}/{m.get('turns_total',0)}")
    parts.append("")

# Input summary
parts.append(f"[INPUT] {inp['total_findings']} findings ({', '.join(f'{k}={v}' for k,v in sorted(sev.items()) if v > 0)})")
srcs = inp["source_files"]
parts.append(f"  Sources: {', '.join(s.split('/')[-1]+'='+str(c) for s,c in sorted(srcs.items()))}\n")

# Python verify
if py_verify:
    status = "PASS" if py_verify.get("issues_found", 0) == 0 else f"{py_verify['issues_found']} ISSUES"
    parts.append(f"[PYTHON VERIFY] {status}")
    for iss in py_verify.get("issues", [])[:3]:
        parts.append(f"  - {iss['check']}: {iss.get('detail','')[:100]}")
    parts.append("")

# Findings by severity
parts.append("[FINDINGS BY SEVERITY]")
for sev_name in ("critical", "high", "medium", "low", "partial", "fail"):
    f_list = [f for f in findings if f.get("severity", "").lower() == sev_name]
    if not f_list:
        continue
    parts.append(f"\n[{sev_name.upper()}] ({len(f_list)}):")
    for f in f_list[:5]:
        fid = f.get("fid", f.get("id", "?"))
        desc = f.get("description", "").replace("\n", " ")[:120]
        parts.append(f"  {fid}: {desc}")
    if len(f_list) > 5:
        parts.append(f"  ... +{len(f_list)-5} more")

user_msg = "\n".join(parts)

# ── Same system prompt as 30B verify ──
SYS_V27 = """You are a final verifier. Review all findings and P-R-J results.

You will receive THREE handoff documents:
1. [LLM-J] — Judge LLM handoff
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-J vs Python. After your final verdict,
write detailed, actionable feedback per model+role.
e.g., P=Qwen30B, R=Qwen14B, J=Selene — separate feedback for each.

Return JSON:
{
  "final_verdict": "approved|approved_with_conditions|rejected",
  "action": "commit|revert|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "3-5 sentences",
  "verification_items": [{"check":"...","result":"pass|fail|partial","detail":"..."}],
  "feedback": {
    "rot1": {
      "P_Qwen30B": {"model":"Qwen30B","role":"proposer","rotation":"rot1","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
      "R_Qwen14B": {"model":"Qwen14B","role":"reflector","rotation":"rot1","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
      "J_Selene": {"model":"Selene","role":"judge","rotation":"rot1","score":0,"strengths":[],"weaknesses":[],"improvements":[]}
    }
  },
  "handoff_comparison": {
    "better_handoff": "llm_j|python|equal",
    "reason": "why one handoff source was more useful for verification",
    "llm_j_strengths": ["..."],
    "python_strengths": ["..."]
  }
}"""

messages = [
    {"role": "system", "content": SYS_V27},
    {"role": "user", "content": user_msg}
]

print(f"[3B] User message: ~{len(user_msg)} chars, {len(findings)} findings")
print(f"[3B] Calling Qwen2.5-Coder-3B on :8082...")

result = call_llm(messages, model="Qwen3B", max_tokens=2048, timeout=600, return_meta=True)

# The model is on :8082, not :8080 — verify
result["model"] = "Qwen2.5-Coder-3B"
result["port"] = 8082
result["prompt_chars"] = len(user_msg)

# Parse and save
content = result["content"]
print(f"[3B] Response: {len(content)} chars")
print(f"[3B] Elapsed: {result.get('elapsed_ms',0)/1000:.1f}s")
print(f"[3B] Tokens: {result.get('usage',{}).get('total_tokens','?')}")

# Try to extract JSON
import re
json_match = re.search(r'(\{.*\})', content, re.DOTALL)
if json_match:
    try:
        parsed = json.loads(json_match.group(1))
        v = parsed.get("final_verdict", "?")
        c = parsed.get("confidence", "?")
        print(f"\n=== 3B VERDICT ===")
        print(f"  Verdict: {v}")
        print(f"  Confidence: {c}")
        print(f"  Summary: {parsed.get('summary','')[:100]}")
        items = parsed.get("verification_items", [])
        if items:
            for item in items[:8]:
                print(f"  [{item.get('result','?')}] {item.get('check','')}")
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")

# Also extract 30B result for comparison
v30 = exp_30b.get("result", {})
print(f"\n=== 30B VERDICT (from exp_30b) ===")
print(f"  Verdict: {v30.get('final_verdict','?')}")
print(f"  Confidence: {v30.get('confidence','?')}")
print(f"  Summary: {v30.get('summary','')[:100]}")
items30 = v30.get("verification_items", [])
for item in items30:
    print(f"  [{item.get('result','?')}] {item.get('check','')}")

with open(OUTPUT_3B, "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"\n[3B] Saved: {OUTPUT_3B}")
