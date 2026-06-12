#!/usr/bin/env python3
# Status: experimental
# Path: none — day_verify(reviewer) 3-model comprehensive comparison
"""Pod A(7B reviewer) 모델 비교: 5개 역할 전체 테스트.
day_verify / rubric / MCP / classify-proposer / classify-judge

MODEL_FILE override + pod restart → 동일 태스크 → 결과 비교"""
import json, os, subprocess, sys, time, urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm

# ── Models ─────────────────────────────────────────────────────
MODELS = [
    {"name": "Coder Q8_0",    "file": "Qwen2.5-Coder-7B-Instruct.Q8_0.gguf",      "size": "7.6G"},
    {"name": "Instruct Q8_0", "file": "Qwen2.5-7B-Instruct-Q8_0.gguf",            "size": "7.6G"},
    {"name": "Instruct Q4",   "file": "Qwen2.5-7B-Instruct-Q4_K_M.gguf",          "size": "4.4G"},
]

# ── 5개 역할 프롬프트 + 테스트 입력 ───────────────────────────

TASKS = [
    {   # 1. day_verify — 최종 검증 보고서
        "role": "day_verify",
        "system": """You are a final verifier. Review all findings and P-R-J results.

Return JSON:
{
  "final_verdict": "approved|rejected|escalate",
  "confidence": 0-100,
  "summary": "1 sentence",
  "reasoning": "2-3 sentences",
  "verification_items": [
    {"check":"API key exposure","result":"pass|fail|partial","detail":"..."},
    {"check":"Memory leak","result":"pass|fail|partial","detail":"..."},
    {"check":"Slow query","result":"pass|fail|partial","detail":"..."}
  ]
}""",
        "user": """Findings reviewed:
1. [critical/security] F001: API key exposed in logs — PASS (already fixed in PR #42)
2. [high/bug] F002: Memory leak in connection pool — FAIL (reproduced, not fixed)
3. [medium/perf] F003: Slow query missing index — PARTIAL (index added, not deployed)""",
        "expected_keys": {"final_verdict", "confidence", "summary", "verification_items"},
        "weight": 1,
    },
    {   # 2. Rubric — 개별 finding 평가
        "role": "rubric",
        "system": """You are a rubric evaluation specialist. Assess each finding against:
- Correctness (0.35): Is this a real, verifiable issue?
- Actionability (0.30): Is there a clear fix?
- Evidence (0.25): Backed by data?
- Novelty (0.10): New insight?

Return JSON:
{
  "rubric_evaluations": [
    {"id":"F001","correctness":0-10,"actionability":0-10,"evidence":0-10,"novelty":0-10,"weighted_score":0.00,"justification":"short"},
    {"id":"F002","correctness":0-10,"actionability":0-10,"evidence":0-10,"novelty":0-10,"weighted_score":0.00,"justification":"short"}
  ]
}""",
        "user": """Evaluate:
[critical/security] F001: API key exposed in error logs — evidence: found in /var/log/app/error.log line 1423
[high/bug] F002: Memory leak in connection pool — evidence: 500 connections not released after timeout""",
        "expected_keys": {"rubric_evaluations"},
        "weight": 1,
    },
    {   # 3. MCP — 메타데이터 생성 (tldr, intent, entities, tags)
        "role": "MCP",
        "system": """You are a conversation analyst preparing structured metadata for MCP.

Output STRICT JSON:
{
  "tldr": "One-line summary (max 15 words)",
  "intent": "question|request|report|clarification|code_change|debug|design|other",
  "entities": {
    "files": ["relative/file/path.py"],
    "technologies": ["Python", "FastAPI"],
    "functions": ["function_name"],
    "mentioned_users": []
  },
  "tags": ["tag1", "tag2"]
}""",
        "user": """=== user_turn ===
로그인 API 응답 시간이 3초나 걸리는데, FastAPI 미들웨어에서 뭔가 문제 있는 것 같아. /api/v1/auth/login 엔드포인트만 유독 느려.

=== thinking ===
사용자가 FastAPI 미들웨어 관련 성능 문제를 보고함. /api/v1/auth/login 경로의 응답 시간이 3초인 점이 특이함. Rate limit 미들웨어나 DB 세션 관련 문제일 가능성.

=== text ===
로그인 API 성능 문제를 분석해보겠습니다. 몇 가지 확인해볼 점:
1. Rate limit 미들웨어에서 대기 시간이 있는지
2. DB connection pool이 고갈되지는 않았는지
3. Redis 세션 캐싱이 정상 동작하는지

auth_routes.py의 login() 함수를 보니, async def login(db: AsyncSession)인데 매 요청마다 새 세션을 열고 있어서 pooling이 안 되고 있네요. 미들웨어보다 DB 세션 문제로 보입니다.""",
        "expected_keys": {"tldr", "intent", "entities", "tags"},
        "weight": 1,
    },
    {   # 4. Classify proposer — finding 생성
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
        "weight": 1,
    },
    {   # 5. Classify judge — 채점 및 결정
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
        "weight": 1,
    },
    {   # 6. MCP — 빈 결과 처리 (엣지 케이스)
        "role": "MCP-edge",
        "system": "Output STRICT JSON with keys: tldr, intent, entities, tags. If no data, empty arrays.",
        "user": "=== user_turn ===\n네\n=== text ===\n알겠습니다.",
        "expected_keys": {"tldr", "intent", "entities", "tags"},
        "weight": 0.5,
    },
    {   # 7. day_verify — 빈 findings (엣지 케이스)
        "role": "verify-edge",
        "system": "Review findings. Return JSON: {\"final_verdict\": \"approved|rejected|escalate\", \"confidence\": 0-100, \"summary\": \"...\", \"verification_items\": []}",
        "user": "Findings: (no findings to review)",
        "expected_keys": {"final_verdict", "confidence", "summary", "verification_items"},
        "weight": 0.5,
    },
]

# ── Helpers ────────────────────────────────────────────────────

def log(msg):
    print(f"  {msg}", flush=True)

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
                log(f"Ready after {i+1}s")
                time.sleep(5)
                return True
        except Exception:
            pass
        if i % 30 == 0 and i > 0:
            log(f"... ({i+1}s)")
        time.sleep(2)
    return False

def restore_default():
    with open("/opt/ai_data/scripts/current-mode-pod-a.env", "w") as f:
        f.write("MODE=day\n")
    subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-a"],
                   capture_output=True, timeout=120)

def run_task(system: str, user: str, timeout: int = 120) -> dict:
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
        parsed = parse_llm_json(raw)
        return {"ok": parsed is not None, "parsed": parsed, "elapsed_s": round(elapsed, 1)}
    except Exception as e:
        elapsed = time.monotonic() - t0
        return {"ok": False, "error": str(e), "elapsed_s": round(elapsed, 1)}

def score_task(parsed: dict, task: dict) -> dict:
    if parsed is None:
        return {"score": 0, "detail": "no_parse"}
    exp_keys = task.get("expected_keys", set())
    found = exp_keys & set(parsed.keys())
    missing = exp_keys - set(parsed.keys())
    if not missing:
        return {"score": 100, "detail": "all_keys_present"}
    score = max(0, 100 - len(missing) * 25)
    return {"score": score, "detail": f"missing={sorted(missing)}"}

def print_bar(score):
    bars = "█" * int(score / 10) + "░" * (10 - int(score / 10))
    return bars

# ── Main ───────────────────────────────────────────────────────

print("=" * 70)
print("  [Pod A 7B] 5개 역할 × 3개 모델 종합 비교")
print(f"  {len(TASKS)} tasks, {len(MODELS)} models")
print("  Roles: day_verify / rubric / MCP / classify-P / classify-J / edge cases")
print("=" * 70)

all_results = {}

for mi, model in enumerate(MODELS):
    print(f"\n{'─'*70}")
    print(f"  [{mi+1}/{len(MODELS)}] {model['name']} ({model['file']})")
    print(f"{'─'*70}")

    ok = restart_pod_a(model["file"])
    if not ok:
        log(f"FAILED: Pod A won't start")
        all_results[model["name"]] = None
        continue

    task_results = []
    for ti, task in enumerate(TASKS):
        role = task["role"]
        print(f"\n  [{ti+1}/{len(TASKS)}] [{role}] ", end="")
        sys.stdout.flush()

        r = run_task(task["system"], task["user"])
        parsed = r.get("parsed")
        grading = score_task(parsed, task)

        elapsed = r.get("elapsed_s", 0)
        s = grading["score"]
        icon = "✅" if s >= 80 else "⚠️" if s > 0 else "❌"
        detail = grading["detail"]

        print(f"{icon} score={s} ({elapsed}s) {detail}", flush=True)

        task_results.append({
            "role": role,
            "ok": parsed is not None,
            "score": s,
            "elapsed_s": elapsed,
            "error": r.get("error", ""),
        })

    all_results[model["name"]] = task_results

# ── Summary ─────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("  최종 비교")
print(f"{'='*70}")

for model in MODELS:
    name = model["name"]
    tr = all_results.get(name)
    if tr is None:
        print(f"\n  {name}: FAILED")
        continue
    total_w = sum(t["score"] * TASKS[ti]["weight"] for ti, t in enumerate(tr))
    total_weight = sum(t["weight"] for t in TASKS)
    w_avg = total_w / total_weight
    avg_t = sum(t["elapsed_s"] for t in tr) / len(tr)
    num_ok = sum(1 for t in tr if t["ok"])
    print(f"\n  {name} ({model['size']})")
    print(f"    Overall: {w_avg:.0f}%  |  Tasks: {num_ok}/{len(tr)}  |  Avg: {avg_t:.0f}s")
    print(f"    {print_bar(w_avg)}")

# Per-role detail
print(f"\n{'─'*70}")
print(f"  역할별 상세")
print(f"{'─'*70}")
print(f"{'Role':<18} {'Coder Q8_0':>16} {'Instr Q8_0':>16} {'Instr Q4':>16}")
print(f"{'─'*66}")
for ti, task in enumerate(TASKS):
    role = task["role"]
    vals = []
    for model in MODELS:
        tr = all_results.get(model["name"])
        if tr and ti < len(tr):
            t = tr[ti]
            icon = "✅" if t["score"] >= 80 else "⚠️" if t["score"] > 0 else "❌"
            vals.append(f"{icon}{t['score']:>3}/{t['elapsed_s']:>4.0f}s")
        else:
            vals.append("   FAILED")
    print(f"{role:<18} {'|'.join(f'{v:^16}' for v in vals)}")

restore_default()
print(f"\n  Pod A 복원 완료 (Coder Q8_0)")
print(f"{'='*70}")
