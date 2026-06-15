#!/usr/bin/env python3
# Status: experimental
# Path: none — Coder Q8_0 retry with longer timeout
"""Coder Q8_0만 classify-P / classify-J / edge cases 재테스트 (timeout=300s)."""
import json, os, subprocess, sys, time, urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.test_common import test_setup, test_heartbeat, test_complete, log, call_llm, parse_llm_json

MODEL = "Qwen2.5-Coder-7B-Instruct.Q8_0.gguf"

TASKS = [
    {   # classify-P
        "role": "classify-P",
        "system": """You are a code review assistant. Examine the turn and extracted facts below.
Generate findings about potential issues, bugs, or improvements.

CRITICAL RULES:
- Each finding MUST cite specific evidence from the extracted facts.
- If no clear issue exists, return an empty findings list.
- Do NOT fabricate code, file paths, or function names.
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
        "user": """Extracted facts:
- login API response time is 3 seconds
- /api/v1/auth/login endpoint is specifically slow
- Rate limit middleware, DB session, and Redis caching are suspected
- auth_routes.py login() opens a new async session per request — no pooling
- DB connection pool exhaustion could be the root cause""",
        "expected_keys": {"findings"},
    },
    {   # classify-J
        "role": "classify-J",
        "system": """You are a scoring judge. Review the findings and verdicts.
Assign a simple score and make a decision.

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
    "top_issues": ["most critical finding in 1 line"]
  }
}""",
        "user": """Reviewer proposed:
D001 [high/bug]: Login endpoint slow due to no DB connection pooling — evidence: auth_routes.py opens new session per request

Reflector verdicts:
D001 → ACCEPT: evidence clearly shows missing session pooling in auth_routes.py login()""",
        "expected_keys": {"P_score", "R_score", "decision", "report"},
    },
    {   # MCP-edge (짧은입력) — 이전에 0.0s 즉시실패
        "role": "MCP-edge",
        "system": "Output STRICT JSON with keys: tldr, intent, entities, tags. If no data, empty arrays.",
        "user": "=== user_turn ===\n네\n=== text ===\n알겠습니다.",
        "expected_keys": {"tldr", "intent", "entities", "tags"},
    },
    {   # verify-edge (빈 findings)
        "role": "verify-edge",
        "system": "Review findings. Return JSON: {\"final_verdict\": \"approved|rejected|escalate\", \"confidence\": 0-100, \"summary\": \"...\", \"verification_items\": []}",
        "user": "Findings:\n(no findings to review)",
        "expected_keys": {"final_verdict", "confidence", "summary", "verification_items"},
    },
]

def restart_pod_a(model_file: str) -> bool:
    env_file = "/opt/ai_data/scripts/current-mode-pod-a.env"
    with open(env_file, "w") as f:
        f.write(f"MODE=day\nMODEL_FILE={model_file}\n")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a"],
                   capture_output=True, timeout=60)
    time.sleep(3)
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a"],
                   capture_output=True, timeout=60)
    for i in range(180):
        try:
            req = urllib.request.Request("http://127.0.0.1:8082/health")
            resp = urllib.request.urlopen(req, timeout=5)
            if resp.status == 200:
                print(f"  Ready after {i+1}s")
                time.sleep(5)
                return True
        except Exception:
            pass
        if i % 30 == 0 and i > 0:
            print(f"  ... ({i+1}s)")
        time.sleep(2)
    return False

def restore_default():
    with open("/opt/ai_data/scripts/current-mode-pod-a.env", "w") as f:
        f.write("MODE=day\n")

def run_task(system: str, user: str, timeout: int = 300) -> dict:
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model="day_verify", max_tokens=512, temperature=0.0,
            timeout=timeout, json_mode=True, return_meta=True,
        )
        elapsed = time.monotonic() - t0
        raw = meta.get("content", "")
        raw_preview = raw[:300]
        parsed = parse_llm_json(raw)
        return {"ok": parsed is not None, "parsed": parsed, "elapsed_s": round(elapsed, 1), "raw_preview": raw_preview}
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {"ok": False, "error": str(e), "elapsed_s": round(elapsed, 1), "raw_preview": str(e)[:200]}

def score_task(parsed: dict, task: dict) -> dict:
    if parsed is None:
        return {"score": 0, "detail": "no_parse"}
    exp_keys = task.get("expected_keys", set())
    found = exp_keys & set(parsed.keys())
    missing = exp_keys - set(parsed.keys())
    if not missing:
        return {"score": 100, "detail": f"ALL({len(found)} keys)"}
    return {"score": max(0, 100 - len(missing) * 25), "detail": f"missing={sorted(missing)}"}

TEST = test_setup("day_verify_retry", "Coder Q8_0 retry with longer timeout")

print("=" * 65)
print("  Coder Q8_0 retry: timeout=300s (480s)")
print(f"  {len(TASKS)} edge-case tasks")
print("=" * 65)

ok = restart_pod_a(MODEL)
if not ok:
    print("FAILED: Pod A won't start")
    sys.exit(1)

for ti, task in enumerate(TASKS):
    role = task["role"]
    print(f"\n  [{ti+1}/{len(TASKS)}] [{role}] timeout=300s")
    sys.stdout.flush()

    r = run_task(task["system"], task["user"])
    parsed = r.get("parsed")
    grading = score_task(parsed, task)

    elapsed = r.get("elapsed_s", 0)
    s = grading["score"]
    icon = "✅" if s >= 80 else "⚠️" if s > 0 else "❌"
    print(f"  → {icon} score={s} ({elapsed}s) {grading['detail']}")

    raw = r.get("raw_preview", "")
    if raw:
        print(f"    raw: {raw[:200]}")

restore_default()
print(f"\n{'='*65}")
print("  Done")
test_complete("retry tests done")
