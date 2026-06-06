#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""DSV2 Lite vs 30B — MCP extract verify 비교"""
import json, sys, os, time, urllib.request, urllib.error
sys.path.insert(0, "/opt/projects/server/scripts")
from lib.llm_client import call_llm

EXPER_DIR = "/opt/projects/server/data/experiment"
Eval30B = "/opt/projects/server/data/eval/eval_30b_verify.json"
OUTPUT_DSV2 = os.path.join(EXPER_DIR, "exp_dsv2_verify_mcp.json")

# ── Load 30B verify results ──
with open(Eval30B) as f:
    v30 = json.load(f)

# ── Build input prompt: 4 extract turns → MCP evaluation ──
prompt = """You are a MCP quality verifier. Review the following 4 extract results and evaluate MCP field accuracy.

Return JSON with:
1. mcp_field_accuracy: intent_accuracy, tldr_accuracy, entity_accuracy per turn
2. findings: list of issues found (id, severity, component, evidence, root_cause)
3. retry_chain_analysis: 3B success rate, 30B fallback rate
4. summary: overall verdict and confidence

Extract Results:

=== Turn 279 (3B 성공) ===
user: "hi" (2 chars)
assistant: "Hello! How can I assist you today?" (34 chars)
extract_model: Qwen3-4B
MCP: intent="report", tags=["greeting","welcome","assistant-response"]
TLDR: "User greeted the assistant"
facts: user fact 0개, text fact 1개 ("Hello! How can I assist you today?")

=== Turn 280 (3B 성공) ===
user: "1+1 답만 해줘" (9 chars)
assistant: "2" (1 char)
extract_model: Qwen3-4B
MCP: intent="answer", tags=["math","short-answer"]
TLDR: "User asked for 1+1 answer"
facts: user fact 0개, text fact 1개 ("2")

=== Turn 281 (3B 2회실패 → 30B fallback) ===
user: 176 chars (code_mod 통합 요청)
assistant: 2490 chars (상세 응답 - 구조, 파일 리스트)
extract_model: 30B (fallback)
MCP files: ["de_mod_pipeline.py", "extract_pipeline.py", "runner.py"]
MCP functions: ["save_result", "upload_review_bundle", "upload_raw"]
TLDR: "User requested code_mod pipeline integration"
facts: user 2개 ("code_mod 통합 필요", "CLI upload 기능"), text 0개

=== Turn 282 (3B 2회실패 → 30B fallback) ===
user: "이 서버의 IP 주소를 알려줘" (16 chars)
assistant: "저는 이 서버의 IP 주소를 알 수 없습니다. 보안상의 이유로 IP 주소를 제공할 수 없습니다." (52 chars)
extract_model: 30B (fallback)
MCP: intent="report", tags=["request","ip-address","security"]
TLDR: "User requested server IP address"
facts: user 0개, text 0개

Evaluate MCP field accuracy per turn:
- intent: is the intent label correct?
- tldr: is the TLDR accurate?
- entity files: are file paths correct?
- entity functions: are function names correct?
- tags: are tags appropriate?
- extract completeness: were all important facts extracted?

Return format:
{
  "summary": {"verdict": "approved|approved_with_conditions|rejected", "confidence": 0-100},
  "mcp_field_accuracy": {
    "total_reviewed": 4,
    "tldr_accuracy": "...",
    "intent_accuracy": "...",
    "entity_files_accuracy": "...",
    "entity_functions_accuracy": "...",
    "tag_relevance": "..."
  },
  "retry_chain_analysis": {
    "3B_success_rate": "...",
    "30B_fallback_rate": "...",
    "fallback_completeness": "..."
  },
  "findings": [
    {"id": "D01", "severity": "high|medium|low", "component": "...", "evidence": "...", "root_cause": "..."}
  ]
}"""

messages = [
    {"role": "system", "content": "You are an MCP quality verifier. Evaluate extract result accuracy. Return ONLY valid JSON."},
    {"role": "user", "content": prompt}
]

print("[DSV2] Calling DSV2 Lite on :8080...")
sys.stdout.flush()

t0 = time.time()
result = call_llm(messages, model="DeepSeekV2Lite", max_tokens=4096, timeout=900, return_meta=True)
elapsed = time.time() - t0

content = result["content"]
print(f"[DSV2] Response: {len(content)} chars, {elapsed:.1f}s")

# Parse JSON
import re
json_match = re.search(r'(\{.*\})', content, re.DOTALL)
if json_match:
    try:
        dsv2_out = json.loads(json_match.group(1))
    except json.JSONDecodeError as e:
        print(f"  JSON error: {e}")
        dsv2_out = {}
else:
    dsv2_out = {"_raw": content[:500]}

print(f"\n=== DSV2 Lite VERDICT ===")
print(f"  Verdict: {dsv2_out.get('summary',{}).get('verdict','?')}")
print(f"  Confidence: {dsv2_out.get('summary',{}).get('confidence','?')}")
mcp_acc = dsv2_out.get('mcp_field_accuracy', {})
for k, v in mcp_acc.items():
    print(f"  {k}: {v}")
retry = dsv2_out.get('retry_chain_analysis', {})
for k, v in retry.items():
    print(f"  {k}: {v}")

dsv2_findings = dsv2_out.get('findings', [])
print(f"\n  Findings: {len(dsv2_findings)}개")
for f in dsv2_findings:
    print(f"    {f.get('id','?')} [{f.get('severity','?')}] {f.get('component','?')}")

# ── COMPARE with 30B ──
print(f"\n{'='*60}")
print(f"COMPARISON: DSV2 Lite vs 30B")
print(f"{'='*60}")

print(f"\n-- 30B Verify Results --")
print(f"  Verdict: approved_with_conditions (confidence=85)")
print(f"  Findings: 7개 (3 high, 2 medium, 2 low)")
v30_findings = v30.get('findings', [])
for f in v30_findings:
    print(f"    {f['id']} [{f['severity']}] {f['component']}: {f['evidence'][:60]}")

print(f"\n-- DSV2 Lite Results --")
print(f"  Verdict: {dsv2_out.get('summary',{}).get('verdict','?')} (confidence={dsv2_out.get('summary',{}).get('confidence','?')})")
print(f"  Findings: {len(dsv2_findings)}개")
for f in dsv2_findings:
    print(f"    {f.get('id','?')} [{f.get('severity','?')}] {f.get('component','?')}: {f.get('evidence','')[:60]}")

# Timing comparison
v30_elapsed = 485742.633 / 1000  # from exp_30b result
print(f"\n-- Performance --")
print(f"  30B:     {v30_elapsed:.1f}s (Qwen30B, 16GB)")
print(f"  DSV2:    {elapsed:.1f}s (DeepSeek-Coder-V2-Lite Q8, 16GB)")
print(f"  Ratio:   {v30_elapsed/elapsed:.1f}x")

# Save
result["model"] = "DeepSeekV2Lite"
result["comparison"] = {
    "30b_verdict": "approved_with_conditions",
    "30b_confidence": 85,
    "30b_findings_count": 7,
    "30b_elapsed_s": round(v30_elapsed, 1),
    "dsv2_verdict": dsv2_out.get('summary',{}).get('verdict','?'),
    "dsv2_confidence": dsv2_out.get('summary',{}).get('confidence','?'),
    "dsv2_findings_count": len(dsv2_findings),
    "dsv2_elapsed_s": round(elapsed, 1),
}
with open(OUTPUT_DSV2, "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)
print(f"\n[DSV2] Saved: {OUTPUT_DSV2}")
