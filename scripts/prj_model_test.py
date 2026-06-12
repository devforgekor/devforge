#!/usr/bin/env python3
# Status: experimental
# Path: none — P/R/J 3-model comparison: Mistral(P) vs Qwen2.5-7B-Instruct(R) vs Llama 3.1 8B(J)
"""P(Proposer)/R(Refuter)/J(Judge) 역할별 추천 모델 검증.
각 모델을 Pod A에 로드 → 해당 역할 프롬프트 실행 → 정확도/속도 측정."""
import json, os, subprocess, sys, time, urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm

# ── 3 models, 3 roles ──────────────────────────────────────────
# Format: {model_file: {role: (system_prompt, user_input, expected_keys)}}

TEST_PLAN = {
    "P": {
        "model_file": "Mistral-7B-Instruct-v0.3-Q8_0.gguf",
        "model_name": "Mistral 7B Instruct Q8_0",
        "system": """You are a code review assistant. Examine the turn and extracted facts below.
Generate findings about potential issues, bugs, or improvements.

CRITICAL RULES:
- Each finding MUST cite specific evidence from the extracted facts.
- If no clear issue exists, return an empty findings list.
- Do NOT fabricate code, file paths, or function names.
- Do NOT guess. If uncertain, leave it out.
- Maximum 20 findings per turn.

Return JSON:
{
  "findings": [
    {
      "id": "D001",
      "severity": "critical|high|medium|low",
      "category": "bug|security|data_loss|performance|quality",
      "description": "1 sentence, under 150 chars",
      "evidence": "quote from extracted facts"
    }
  ]
}""",
        "cases": [
            {
                "label": "정상-성능이슈",
                "user": """Extracted facts:
- login API response time is 3 seconds
- /api/v1/auth/login endpoint is specifically slow
- auth_routes.py login() opens a new async session per request — no pooling""",
                "expected_keys": {"findings"},
            },
            {
                "label": "보안이슈",
                "user": """Extracted facts:
- API keys are logged to /var/log/app/error.log on authentication failure
- Log retention policy: 90 days
- Logs are accessible by all developers in the team""",
                "expected_keys": {"findings"},
            },
            {
                "label": "이슈없음",
                "user": """Extracted facts:
- CI pipeline completes in 2 minutes
- All tests pass
- Code coverage is 87%""",
                "expected_keys": {"findings"},
            },
        ],
    },
    "R": {
        "model_file": "Qwen2.5-7B-Instruct-Q8_0.gguf",
        "model_name": "Qwen 2.5 7B Instruct Q8_0",
        "system": """You are a verdict reviewer. For each finding proposed by the reviewer,
decide ACCEPT or REJECT based ONLY on whether the evidence supports the finding.

CRITICAL RULES:
- ACCEPT: evidence clearly supports the finding.
- REJECT: evidence is weak, missing, or contradicts the finding.
- REJECT: duplicate of another finding.
- Do NOT add new findings or modify existing ones.
- Be concise. One sentence per verdict.

Return JSON:
{
  "verdicts": [
    {"id": "D001", "verdict": "accept", "reason": "evidence supports this finding"},
    {"id": "D002", "verdict": "reject", "reason": "evidence does not support"}
  ]
}""",
        "cases": [
            {
                "label": "정상-수용",
                "user": """Proposed findings:
D001 [high/bug]: Login endpoint slow due to no DB connection pooling — evidence: auth_routes.py opens new session per request
D002 [critical/security]: API keys exposed in logs — evidence: keys logged on auth failure to /var/log/app/error.log""",
                "expected_keys": {"verdicts"},
            },
            {
                "label": "일부-기각",
                "user": """Proposed findings:
D001 [high/bug]: Memory usage at 87% is critical — evidence: available memory is 10Gi out of 22Gi
D002 [low/style]: Variable naming inconsistent — evidence: both camelCase and snake_case used
D003 [high/security]: Password stored in plaintext — evidence: password column exists in users table""",
                "expected_keys": {"verdicts"},
            },
            {
                "label": "할루시네이션-탐지",
                "user": """Proposed findings:
D001 [critical/bug]: The server crashed 3 times today — evidence: uptime is 1d 14h 51m
D002 [medium/security]: Root login detected — evidence: last login shows user 'opc'
D003 [high/performance]: 30B model inference timeout — evidence: Pod B runs Qwen3-Coder-30B-A3B""",
                "expected_keys": {"verdicts"},
            },
        ],
    },
    "J": {
        "model_file": "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf",
        "model_name": "Llama 3.1 8B Instruct Q8_0",
        "system": """You are a scoring judge. Review the findings and verdicts.
Assign a simple score and make a decision.

CRITICAL RULES:
- P_score (0-30) = quality of findings (correctness + coverage + precision)
- R_score (0-30) = quality of verdicts (accuracy + efficiency)
- decision = APPROVED if majority accepted and no critical findings rejected
- decision = REJECT otherwise

Return JSON:
{
  "P_score": 0-30,
  "P_rubric": {"correctness": 0-10, "coverage": 0-10, "precision": 0-10},
  "R_score": 0-30,
  "R_rubric": {"accuracy": 0-10, "efficiency": 0-10},
  "decision": "APPROVED|REJECT",
  "consensus_score": 0-100,
  "approved": ["D001"],
  "rejected": ["D002"],
  "report": {
    "summary": "1 sentence",
    "top_issues": ["most critical in 1 line"]
  }
}""",
        "cases": [
            {
                "label": "정상-판정",
                "user": """Reviewer proposed:
D001 [high/bug]: Login endpoint slow due to no DB connection pooling
D002 [critical/security]: API keys exposed in logs

Reflector verdicts:
D001 → ACCEPT: evidence shows no session pooling in auth_routes.py
D002 → ACCEPT: keys found in error.log on auth failure""",
                "expected_keys": {"P_score", "R_score", "decision", "report"},
            },
            {
                "label": "일부-기각-판정",
                "user": """Reviewer proposed:
D001 [low/style]: Variable naming inconsistent — camelCase and snake_case mixed
D002 [high/bug]: Memory leak in connection pool

Reflector verdicts:
D001 → REJECT: naming inconsistency is a style preference, not a bug
D002 → ACCEPT: connections not released after timeout in pool.py""",
                "expected_keys": {"P_score", "R_score", "decision", "report"},
            },
            {
                "label": "할루시네이션-판별",
                "user": """Reviewer proposed:
D001 [critical/bug]: Server crashed 3 times today — evidence shown in findings

Reflector verdicts:
D001 → REJECT: server uptime is 1d 14h 51m, no crashes recorded in logs""",
                "expected_keys": {"P_score", "R_score", "decision", "report"},
            },
        ],
    },
}

# ── Helpers ────────────────────────────────────────────────────

TIMEOUT = 300  # 5min per call

def log(msg):
    print(f"  {msg}", flush=True)

def restart_pod_a(model_file: str) -> bool:
    env_file = "/opt/ai_data/scripts/current-mode-pod-a.env"
    with open(env_file, "w") as f:
        f.write(f"MODE=day\nMODEL_FILE={model_file}\n")
    log(f"Stopping Pod A...")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a"],
                   capture_output=True, timeout=60)
    time.sleep(3)
    log(f"Starting with {model_file}...")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a"],
                   capture_output=True, timeout=60)
    log("Waiting for :8082 health...")
    for i in range(300):
        try:
            req = urllib.request.Request("http://127.0.0.1:8082/health")
            resp = urllib.request.urlopen(req, timeout=5)
            if resp.status == 200:
                log(f"Ready after {i+1}s")
                time.sleep(5)
                return True
        except Exception:
            pass
        if i % 30 == 0 and i > 0:
            log(f"... {i+1}s")
        time.sleep(2)
    return False

def restore_coder():
    with open("/opt/ai_data/scripts/current-mode-pod-a.env", "w") as f:
        f.write("MODE=day\n")
    subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-a"],
                   capture_output=True, timeout=120)

def run_case(system: str, user: str) -> dict:
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model="day_verify", max_tokens=512, temperature=0.0,
            timeout=TIMEOUT, json_mode=True, return_meta=True,
        )
        elapsed = time.monotonic() - t0
        raw = meta.get("content", "")
        parsed = parse_llm_json(raw)
        return {"ok": parsed is not None, "parsed": parsed, "elapsed_s": round(elapsed, 1), "raw_preview": raw[:200]}
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {"ok": False, "error": str(e), "elapsed_s": round(elapsed, 1), "raw_preview": ""}

def score_case(parsed: dict, expected_keys: set) -> dict:
    if parsed is None:
        return {"score": 0, "detail": "no_parse"}
    found = expected_keys & set(parsed.keys())
    missing = expected_keys - set(parsed.keys())
    if not missing:
        return {"score": 100, "detail": f"ALL({len(found)})"}
    return {"score": max(0, 100 - len(missing) * 25), "detail": f"missing={sorted(missing)}"}

def print_table(results):
    roles = ["P", "R", "J"]
    models = [TEST_PLAN[r]["model_name"][:18] for r in roles]
    header = f"{'Role':<8} {'Case':<18} {'|'.join(f'{m:^16}' for m in models)}"
    print(header)
    print("-" * len(header))

    for ri, role in enumerate(roles):
        plan = TEST_PLAN[role]
        for ci, case in enumerate(plan["cases"]):
            label = case["label"]
            vals = []
            for rj, r2 in enumerate(roles):
                tr = results.get(r2, [])
                if ci < len(tr):
                    t = tr[ci]
                    icon = "✅" if t["score"] >= 80 else "⚠️" if t["score"] > 0 else "❌"
                    spd = f"{t['elapsed_s']:>4.0f}s" if t["elapsed_s"] > 0 else "--  "
                    vals.append(f"{icon}{t['score']:>3}/{spd}")
                else:
                    vals.append(f"{'N/A':^16}")
            print(f"{role:<8} {label:<18} {'|'.join(f'{v:^16}' for v in vals)}")

# ── Main ───────────────────────────────────────────────────────

print("=" * 70)
print("  P/R/J 3-Model Comparison")
print(f"  Timeout per call: {TIMEOUT}s")
print("=" * 70)

results = {}  # role → [case_result, ...]

for role_key in ["P", "R", "J"]:
    plan = TEST_PLAN[role_key]
    name = plan["model_name"]
    model_file = plan["model_file"]
    cases = plan["cases"]

    print(f"\n{'='*70}")
    print(f"  [{role_key}] {name} ({model_file}) — {len(cases)} cases")
    print(f"{'='*70}")

    # Check file exists
    model_path = f"/opt/ai_data/models/gguf/{model_file}"
    if not os.path.exists(model_path):
        log(f"FILE NOT FOUND: {model_path}")
        results[role_key] = [{"score": 0, "elapsed_s": 0, "ok": False, "error": "model not found"} for _ in cases]
        continue

    ok = restart_pod_a(model_file)
    if not ok:
        log(f"FAILED: Pod A won't start with {model_file}")
        results[role_key] = [{"score": 0, "elapsed_s": 0, "ok": False, "error": "Pod A failed"} for _ in cases]
        continue

    role_results = []
    for ci, case in enumerate(cases):
        label = case["label"]
        print(f"\n  [{role_key}] [{ci+1}/{len(cases)}] {label}... ", end="")
        sys.stdout.flush()

        r = run_case(plan["system"], case["user"])
        parsed = r.get("parsed")
        grading = score_case(parsed, case["expected_keys"])

        elapsed = r.get("elapsed_s", 0)
        s = grading["score"]
        icon = "✅" if s >= 80 else "⚠️" if s > 0 else "❌"
        print(f"{icon} score={s} ({elapsed}s) {grading['detail']}", flush=True)

        if s < 100 and r.get("raw_preview"):
            print(f"       raw: {r['raw_preview'][:120]}")

        role_results.append({
            "role": role_key,
            "case": label,
            "ok": parsed is not None,
            "score": s,
            "elapsed_s": elapsed,
            "error": r.get("error", ""),
        })

    results[role_key] = role_results

# ── Summary ────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("  SUMMARY — 각 역할별 추천 모델 성능")
print(f"{'='*70}")

for role_key in ["P", "R", "J"]:
    plan = TEST_PLAN[role_key]
    name = plan["model_name"]
    tr = results.get(role_key, [])
    if not tr:
        print(f"\n  [{role_key}] {name}: NO DATA")
        continue
    avg_s = sum(t["score"] for t in tr) / len(tr)
    avg_t = sum(t["elapsed_s"] for t in tr) / len(tr)
    num_ok = sum(1 for t in tr if t["ok"])
    bar = "█" * int(avg_s / 10) + "░" * (10 - int(avg_s / 10))
    print(f"\n  [{role_key}] {name}")
    print(f"    Score: {avg_s:.0f}%  |  {num_ok}/{len(tr)} passed  |  Avg: {avg_t:.0f}s/call")
    print(f"    {bar}")

print(f"\n{'─'*70}")
print("  역할별 상세 비교")
print(f"{'─'*70}")
print_table(results)

print(f"\n{'─'*70}")
restore_coder()
print("  Pod A restored to Coder Q8_0")
print(f"{'='*70}")
