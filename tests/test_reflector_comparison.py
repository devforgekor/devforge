#!/usr/bin/env python3
# Status: experimental
# Path: tests/test_reflector_comparison.py — pytest
"""14B Q4_K_M vs Q8_0 — R 역할 비교 테스트

동일한 P findings (12개)를 Q8_0 모델에 투입, Q4_K_M 결과와 비교.
"""
import json, time, os, sys, requests

API_URL = "http://127.0.0.1:8080/v1/chat/completions"
EXPER_DIR = "/opt/projects/server/data/experiment"

# 1. Load original P findings
with open(f"{EXPER_DIR}/exp_p_rot1_r1_norubric_r1_norubric.json") as f:
    p_data = json.load(f)
p_findings = p_data["result"]["findings"]

# 2. Load original R result (Q4_K_M baseline)
with open(f"{EXPER_DIR}/exp_r_rot1_r1_norubric_r1_norubric.json") as f:
    r_q4_data = json.load(f)
r_q4_verdicts = r_q4_data["result"].get("verdicts", [])

# 3. System prompt (same as original)
REFLECTOR_SYSTEM_PROMPT = """You are a review reflector. For each finding submitted by the Proposer, decide ACCEPT or REJECT. Be precise -- if the finding is valid, ACCEPT it. If it is not a real issue or duplicates another, REJECT it.

Return JSON:
{
  "verdicts": [
    {"id": "F001", "verdict": "accept", "reason": "concise justification"},
    {"id": "F002", "verdict": "reject", "reason": "concise justification"}
  ]
}"""

# 4. Call Q8_0
user_text = f"Proposer findings:\n{json.dumps(p_findings, ensure_ascii=False, indent=2)}"
messages = [
    {"role": "system", "content": REFLECTOR_SYSTEM_PROMPT},
    {"role": "user", "content": user_text},
]

print("=" * 60)
print("14B Q4_K_M vs Q8_0 — R 역할 비교 테스트")
print(f"Findings: {len(p_findings)}개")
print("=" * 60)

# Check API health first
try:
    r = requests.get("http://127.0.0.1:8080/health", timeout=5)
    print(f"\n[health] :8080 → {r.status_code}")
except Exception as e:
    print(f"\n[ERROR] :8080 not reachable: {e}")
    sys.exit(1)

# Call
t0 = time.time()
payload = {
    "messages": messages,
    "max_tokens": 1024,
    "temperature": 0.1,
    "stream": False,
}
try:
    r = requests.post(API_URL, json=payload, timeout=1200)
    t1 = time.time()
    elapsed = t1 - t0
    print(f"\n[response] {r.status_code} in {elapsed:.1f}s")
    r.raise_for_status()
    body = r.json()
except Exception as e:
    print(f"[ERROR] API call failed: {e}")
    sys.exit(1)

# Parse result
content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
usage = body.get("usage", {})
timings = body.get("timings", {})

print(f"\n[usage] prompt={usage.get('prompt_tokens','?')} completion={usage.get('completion_tokens','?')} total={usage.get('total_tokens','?')}")
if timings:
    pps = timings.get("prompt_per_second", 0)
    tps = timings.get("predicted_per_second", 0)
    print(f"[speed] prompt={pps:.1f} t/s  gen={tps:.1f} t/s")

# Parse JSON from response
stripped = content.strip()
if stripped.startswith("```"):
    lines = stripped.split("\n")
    stripped = "\n".join(l for l in lines if not l.startswith("```"))
try:
    result = json.loads(stripped)
    q8_verdicts = result.get("verdicts", [])
    print(f"\n[Q8_0] {len(q8_verdicts)} verdicts")
except json.JSONDecodeError as e:
    print(f"[ERROR] JSON parse failed: {e}")
    print(f"Raw: {stripped[:500]}")
    q8_verdicts = []

# 5. Compare
print("\n" + "=" * 60)
print("비교 결과: Q4_K_M vs Q8_0")
print("=" * 60)

q4_map = {v.get("id"): v for v in r_q4_verdicts}
q8_map = {v.get("id"): v for v in q8_verdicts}

all_ids = sorted(set(list(q4_map.keys()) + list(q8_map.keys())))
matching = 0
diff = 0
q4_only = 0
q8_only = 0

print(f"\n{'ID':<8} {'Q4_K_M':<12} {'Q8_0':<12} {'Match?':<8}")
print("-" * 44)
for fid in all_ids:
    q4v = q4_map.get(fid, {}).get("verdict", "-")
    q8v = q8_map.get(fid, {}).get("verdict", "-")
    match = "✅" if q4v == q8v else "❌"
    if q4v == q8v:
        matching += 1
    else:
        diff += 1
    if fid not in q4_map:
        q4_only += 1
    if fid not in q8_map:
        q8_only += 1
    r1 = q4_map.get(fid, {}).get("reason", "")[:40]
    r2 = q8_map.get(fid, {}).get("reason", "")[:40]
    print(f"{fid:<8} {q4v:<12} {q8v:<12} {match:<8}")

total = len(all_ids)
print(f"\n일치: {matching}/{total}")
print(f"불일치: {diff}/{total}")
print(f"Agreement rate: {matching/total*100:.0f}%" if total > 0 else "")
print(f"\nQ4_K_M elapsed: {r_q4_data.get('elapsed_ms',0)/1000:.0f}s")
print(f"Q8_0 elapsed:   {elapsed:.0f}s")

# Save result
result_file = f"{EXPER_DIR}/exp_r_q8_comparison.json"
out = {
    "meta": {
        "test": "14B Q4_K_M vs Q8_0 R role comparison",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    },
    "q4_k_m": {
        "model": "Qwen2.5-Coder-14B-Instruct-Q4_K_M",
        "verdicts": r_q4_verdicts,
        "elapsed_ms": r_q4_data.get("elapsed_ms", 0),
        "usage": r_q4_data.get("usage", {}),
    },
    "q8_0": {
        "model": "Qwen2.5-Coder-14B-Instruct-Q8_0",
        "verdicts": q8_verdicts,
        "elapsed_ms": elapsed * 1000,
        "usage": usage,
    },
    "comparison": {
        "total_findings": total,
        "matching": matching,
        "diff": diff,
        "agreement_rate": matching / total if total > 0 else 0,
    },
}
with open(result_file, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"\n[SAVED] {result_file}")
