#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_verify_proposer.py — pytest
"""7B Q8_0 → 30B verify 동일 input으로 비교 테스트"""
import json, time, requests, sys, os

API = "http://127.0.0.1:8080/v1/chat/completions"
EXPER_DIR = "/opt/projects/server/data/experiment"

# ── 1. Load experiment data ──
with open(f"{EXPER_DIR}/pipeline_state_r1_norubric.json") as f:
    state = json.load(f)

inp = state["input"]
findings = inp.get("findings", [])
ext = state.get("extract", {})
pv = state.get("python_verify", {})
v30 = state.get("30b_verify", {})

# ── 2. Build context (identical to build_context("30b_verify")) ──
parts = []
parts.append("=== PIPELINE CONTEXT: Round 1 ===\n")

# Extract
if ext:
    models = ext.get("models", [])
    if models:
        parts.append(f"[3B EXTRACT] {len(models)} models x 15 turns")
        for m in models:
            fth = f"{m.get('faithfulness_rate',0)*100:.0f}%"
            sec = m.get('elapsed_seconds', 0)
            ec = m.get('total_extractions', 0)
            parts.append(f"  {m['model']}: faith={fth} ({m.get('total_faithful',0)}/{ec}), time={sec:.0f}s, ok={m.get('turns_ok',0)}/{m.get('turns_total',0)}")
        parts.append("")

# Input summary
sev = inp["severity_distribution"]
parts.append(f"[INPUT] {inp['total_findings']} findings ({', '.join(f'{k}={v}' for k,v in sorted(sev.items()) if v > 0)})")
srcs = inp["source_files"]
parts.append(f"  Sources: {', '.join(s.split('/')[-1]+'='+str(c) for s,c in sorted(srcs.items()))}\n")

# Python verify
if pv and pv.get("total_findings"):
    status = "PASS" if pv.get("issues_found", 0) == 0 else f"{pv['issues_found']} ISSUES"
    parts.append(f"[PYTHON VERIFY] {status}")
    for iss in pv.get("issues", [])[:3]:
        parts.append(f"  - {iss['check']}: {iss.get('detail','')[:100]}")
    parts.append("")

# Findings by severity
parts.append("[FINDINGS BY SEVERITY]")
for sev_name in ("critical", "high", "medium", "low", "partial", "fail"):
    f_list = [f for f in findings if f.get("severity", "").lower() == sev_name]
    if not f_list: continue
    parts.append(f"\n[{sev_name.upper()}] ({len(f_list)}):")
    for f in f_list[:5]:
        fid = f.get("fid", f.get("id", "?"))
        desc = f.get("description", "").replace("\n", " ")[:120]
        parts.append(f"  {fid}: {desc}")
    if len(f_list) > 5:
        parts.append(f"  ... +{len(f_list)-5} more")

user_text = "\n".join(parts)

# ── 3. Same system prompt as 30B verify ──
VERIFIER_SYSTEM_PROMPT = """You are a final verifier. Review all findings and P-R-J results.

You will receive THREE handoff documents:
1. [LLM-J] — Judge LLM handoff
2. [Python] — deterministic handoff
3. [Python consolidated] — full rotation summary

Compare LLM-J vs Python. After your final verdict,
write detailed, actionable feedback per model+role:
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
      "P_Qwen30B": {"model":"proposer","role":"proposer","rotation":"rot1","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
      "R_Qwen14B": {"model":"reflector","role":"reflector","rotation":"rot1","score":0,"strengths":[],"weaknesses":[],"improvements":[]},
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

# ── 4. Call 7B Q8_0 ──
messages = [
    {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
    {"role": "user", "content": user_text},
]

print("=" * 60)
print("7B Q8_0 Verify Test (vs 30B)")
print(f"Input context: {len(user_text)} chars, {len(findings)} findings")
print("=" * 60)

t0 = time.time()
r = requests.post(API, json={
    "messages": messages, "max_tokens": 1024, "temperature": 0.1, "stream": False,
}, timeout=1800)
elapsed = time.time() - t0
print(f"\n[response] {r.status_code} in {elapsed:.0f}s")
r.raise_for_status()
body = r.json()

content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
usage = body.get("usage", {})
timings = body.get("timings", {})
print(f"[usage] prompt={usage.get('prompt_tokens','?')} completion={usage.get('completion_tokens','?')} total={usage.get('total_tokens','?')}")
if timings:
    print(f"[speed] prompt={timings.get('prompt_per_second',0):.1f} t/s  gen={timings.get('predicted_per_second',0):.1f} t/s")

# Parse JSON
stripped = content.strip()
if stripped.startswith("```"):
    lines = stripped.split("\n")
    stripped = "\n".join(l for l in lines if not l.startswith("```"))
result = json.loads(stripped)

# ── 5. Compare with 30B ──
v30_res = v30
print(f"\n{'='*60}")
print(f"비교: 30B vs 7B Q8_0")
print(f"{'='*60}")
print(f"\n{'항목':<30} {'30B':<25} {'7B Q8_0':<25}")
print(f"{'-'*80}")
print(f"{'final_verdict':<30} {str(v30_res.get('final_verdict','?')):<25} {str(result.get('final_verdict','?')):<25}")
print(f"{'action':<30} {str(v30_res.get('action','?')):<25} {str(result.get('action','?')):<25}")
print(f"{'confidence':<30} {str(v30_res.get('confidence','?')):<25} {str(result.get('confidence','?')):<25}")
print(f"{'summary':<30} {v30_res.get('summary','?')[:40]:<25} {result.get('summary','?')[:40]:<25}")

v30_items = v30_res.get("verification_items", [])
q8_items = result.get("verification_items", [])
print(f"\n[verification_items] 30B: {len(v30_items)} | 7B Q8_0: {len(q8_items)}")
for item in v30_items[:5]:
    print(f"  30B [{item.get('result','?')}] {item.get('check','')[:60]}")
print("")
for item in q8_items[:5]:
    print(f"  Q8_0 [{item.get('result','?')}] {item.get('check','')[:60]}")

# Save
result_file = f"{EXPER_DIR}/exp_verify_7b_q8_comparison.json"
out = {
    "meta": {"test": "30B vs 7B Q8_0 verify comparison", "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
    "30b": {"final_verdict": v30_res.get("final_verdict"), "action": v30_res.get("action"), "confidence": v30_res.get("confidence"), "summary": v30_res.get("summary"), "verification_items": v30_items, "elapsed_ms": v30.get("elapsed_ms", 0)},
    "7b_q8_0": {"final_verdict": result.get("final_verdict"), "action": result.get("action"), "confidence": result.get("confidence"), "summary": result.get("summary"), "reasoning": result.get("reasoning"), "verification_items": q8_items, "elapsed_ms": elapsed * 1000, "usage": usage},
}
with open(result_file, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"\n[SAVED] {result_file}")
