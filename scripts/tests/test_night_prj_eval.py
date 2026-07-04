#!/usr/bin/env python3
# Status: experimental
# Path: none — night PRJ GT evaluation: P=30B Q4_K_M → R=14B Q4_K_M → N14B Q6_K(J)
"""Ground truth 기반 night P-R-J 평가. P=30B(P), R=14B(R), J=N14B Q6.
R을 hallucination detector로 활용 (MiniCheck 제거).
night.py의 SYSTEM_PROPOSER/REFUTER/JUDGE (with few-shot) 사용.
inference swap 방식: P→R→J 순차 swap.
점수: GT 5개 시나리오 × P/R/J 각 100점 만점."""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import List, Optional

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm_client import MODEL_REGISTRY
from lib.pod_manager.container import _podman_start_inference, _podman_stop_inference
from lib.test_common import call_llm, log, test_complete, test_setup

# ── Night models (night.py v4.0) ──────────────────────────────────────
# review-p → 30B Q4_K_M (strong reasoning for finding generation)
# review-r → 14B Q4_K_M (fast binary accept/reject decisions)
P_MODE = "review-p"
R_MODE = "review-r"
J_MODE = "review-j"  # N14B Q6_K

# ── Night system prompts (from night.py with few-shot) ─────────────────
SYSTEM_PROPOSER = """You are a code review proposer. Given a structural audit summary, propose findings for deeper investigation.
Each finding must be:
- Specific: reference exact code pattern, file, or logic
- Actionable: clear what should change
- In priority order: critical before minor
- **evidence_quote**: quote the EXACT sentence(s) from the audit text that support this finding. Copy verbatim — this is critical for verification.

Example:
Input: audit shows hardcoded credentials in config files, N+1 queries, and missing CSRF tokens
Output:
{
  "findings": [
    {
      "id": "F01", "type": "security", "severity": "critical",
      "description": "Hardcoded API keys and credentials in source code",
      "rationale": "Exposes credentials to anyone with codebase access",
      "evidence_quote": "hardcoded credentials in config files",
      "proposed_action": "Move to secure environment variables"
    },
    {
      "id": "F02", "type": "bug", "severity": "major",
      "description": "N+1 select problem in user association queries",
      "rationale": "Multiple DB round trips degrade performance under load",
      "evidence_quote": "N+1 queries",
      "proposed_action": "Implement eager loading or batch queries"
    }
  ],
  "summary": "2 findings: 1 critical security, 1 major performance"
}

Output STRICT JSON:
{
  "findings": [
    {"id": "F01", "type": "bug|quality|performance|security|data_loss", "severity": "critical|major|minor", "description": "1-2 sentence description", "rationale": "why this matters", "evidence_quote": "exact sentence from audit text supporting this finding", "proposed_action": "what to do about it"}
  ],
  "summary": "1-sentence overall assessment"
}"""

SYSTEM_REFUTER = """You are a review reflector. Given a set of findings, decide for each finding:
- ACCEPT: the finding is real and correctly identified
- REJECT: the finding is false, irrelevant, or already handled

Example:
Input: {"id": "F01", "type": "security", "severity": "critical", "description": "Hardcoded API keys in source code", "rationale": "Exposes credentials", "proposed_action": "Move to env vars"}
Output: {"verdicts": [{"id": "F01", "verdict": "accept", "reason": "Hardcoded credentials are a real security vulnerability with clear evidence"}]}

Input: {"id": "F03", "type": "bug", "severity": "critical", "description": "review_facts.verdict remains 'pending' and is never updated", "rationale": "Prevents downstream processing", "proposed_action": "Add UPDATE logic to verdict field"}
Output: {"verdicts": [{"id": "F03", "verdict": "reject", "reason": "Verdict 'pending' is by design — downstream pipeline updates it after completion, not a bug"}]}

Input (hallucination overstatement): Memory 12/22GB used, swap 508MB/8GB, load 2.55, /opt/ai_data disk 92%
       Finding: {"id": "F01", "type": "performance", "severity": "critical", "description": "Memory usage critically high, OOM imminent", "rationale": "High memory usage may cause system crash", "evidence_quote": "Memory: 22GB total, 12GB used", "proposed_action": "Add more RAM or reduce workload"}
Output: {"verdicts": [{"id": "F01", "verdict": "reject", "reason": "12/22GB is only 55% usage with ample swap (508MB/8GB) and healthy load (2.55) — not OOM-critical. Finding overstates evidence. The real issue is /opt/ai_data disk at 92%."}]}

Output STRICT JSON:
{
  "verdicts": [
    {"id": "F01", "verdict": "accept", "reason": "1-sentence explanation"},
    {"id": "F02", "verdict": "reject", "reason": "1-sentence explanation"}
  ]
}"""

SYSTEM_JUDGE = """You evaluate a code review pipeline. Score Finder (P) on correctness(0-10), coverage(0-10), precision(0-10). Score Reflector (R) on accuracy(0-10), efficiency(0-10), completeness(0-10). P_score = correctness+coverage+precision, R_score = accuracy+efficiency+completeness.

Scoring rules:
- If P found ZERO findings: P_score=0, R_score=10 (nothing to check)
- If R rejected ALL findings as false: R_score=30 (all-efficient), P drops to low precision
- Normal case: P_score up to 30, R_score up to 30
- decision=APPROVED when overall reasonable, REJECT when pipeline failed

Examples:
Case 1: No issues found → P_score=0(correctness=0+coverage=0+precision=0), R_score=10(accuracy=5+efficiency=5+completeness=0), decision=APPROVED, consensus_score=10
Case 2: 2 findings, both real, both accepted → P_score=27(correctness=9+coverage=8+precision=10), R_score=27(accuracy=9+efficiency=8+completeness=10), decision=APPROVED, consensus_score=84
Case 3: 3 findings, 1 accepted 2 rejected (hallucinations) → P_score=17(correctness=6+coverage=5+precision=6), R_score=24(accuracy=9+efficiency=8+completeness=7), decision=APPROVED, consensus_score=66

Output JSON with P_score, R_score, decision(APPROVED/REJECT), consensus_score(0-100)."""

# ── Ground Truth: 5 scenarios ─────────────────────────────────────────
GT = [
    {
        "id": "perf_bug",
        "title": "성능 버그 (DB connection pool 누수)",
        "user": "로그인 API가 3초나 걸리는데 원인이 뭘까?",
        "think": "사용자가 로그인 API 성능 문제를 보고. 3초는 비정상.",
        "text": "/api/v1/auth/login 확인 결과 auth_routes.py login()이 매 요청마다 새 DB 세션을 염. AsyncSession이지만 연결 풀링이 전혀 안 됨. 이것이 3초 응답의 주 원인.",
        "facts": [
            "login API response time is 3 seconds",
            "/api/v1/auth/login endpoint 응답 시간 3초",
            "auth_routes.py login() opens a new async session per request — no pooling",
            "DB connection pool exhaustion이 근본 원인으로 의심됨",
        ],
        "expect": {
            "p_min": 1,
            "p_cats": ["performance"],
            "p_hallu_max": 20,
            "r_hallu_kw": [],
            "j_dec": "APPROVED",
            "j_p_min": 15,
            "j_p_max": 30,
            "j_r_min": 15,
            "j_r_max": 30,
        },
    },
    {
        "id": "security_leak",
        "title": "보안 이슈 (API 키 로깅 노출)",
        "user": "에러 로그에서 API 키가 발견됐어. 어떻게 해야 할까?",
        "think": "사용자가 API 키 노출 보안 문제를 보고.",
        "text": "Auth 실패 시 stack trace + 요청 파라미터가 /var/log/app/error.log에 기록됨. Authorization 헤더 API key도 함께 로깅. 로그 보존 90일, 모든 개발자 접근 가능.",
        "facts": [
            "API keys logged to /var/log/app/error.log on auth failure",
            "Log retention: 90 days",
            "Logs accessible by all developers",
        ],
        "expect": {
            "p_min": 1,
            "p_cats": ["security"],
            "p_hallu_max": 20,
            "r_hallu_kw": [],
            "j_dec": "APPROVED",
            "j_p_min": 15,
            "j_p_max": 30,
            "j_r_min": 15,
            "j_r_max": 30,
        },
    },
    {
        "id": "no_issue",
        "title": "정상 코드 (이슈 없음)",
        "user": "CI 파이프라인 통과했어. 배포해도 될까?",
        "think": "사용자가 CI 통과 확인, 배포 승인 요청.",
        "text": "CI 파이프라인 2분 완료. 모든 테스트 통과. 코드 커버리지 87%.",
        "facts": [
            "CI pipeline completes in 2 minutes",
            "All tests pass",
            "Code coverage is 87%",
        ],
        "expect": {
            "p_min": 0,
            "p_cats": [],
            "p_hallu_max": 0,
            "r_hallu_kw": [],
            "j_dec": "APPROVED",
            "j_p_min": 0,
            "j_p_max": 10,
            "j_r_min": 0,
            "j_r_max": 10,
        },
    },
    {
        "id": "hallu_trap",
        "title": "할루시네이션 트랩",
        "user": "서버 메모리가 부족한 것 같아. OOM이 날까 걱정이야.",
        "think": "사용자가 메모리 부족을 걱정.",
        "text": "서버 상태: 메모리 22GB 중 12GB 사용, 10GB 여유. 스왑 8GB 중 508MB 사용. 부하 2.55/2.46/4.02. OOM 위험 낮음. /opt/ai_data 디스크 92% — 모델 파일 정리 필요.",
        "facts": [
            "Memory: 22Gi total, 12Gi used, 10Gi available",
            "Swap: 8Gi total, 508Mi used",
            "Load: 1min=2.55, 5min=2.46, 15min=4.02",
            "/opt/ai_data disk at 92% — needs cleanup",
        ],
        "expect": {
            "p_min": 1,
            "p_cats": ["quality"],
            "p_hallu_max": 30,
            "r_hallu_kw": ["crash", "OOM", "out of memory"],
            "j_dec": "APPROVED",
            "j_p_min": 5,
            "j_p_max": 30,
            "j_r_min": 5,
            "j_r_max": 30,
        },
    },
    {
        "id": "mixed",
        "title": "복합 이슈 (스타일+보안+누수)",
        "user": "코드 리뷰 좀 해줘. naming 컨벤션하고 보안 관련해서.",
        "think": "사용자가 naming 컨벤션 + 보안 코드 리뷰 요청.",
        "text": "분석 결과:\n1. user_table/userAccount 혼용 — snake_case/camelCase\n2. users.password 컬럼 plaintext — 해싱 없음\n3. conn.close() try-finally 누락 — connection leak\n4. user_age, userEmail, UserName 혼용",
        "facts": [
            "user_table and userAccount naming mixed — snake_case and camelCase",
            "users.password column plaintext — no password hashing",
            "conn.close() missing in try-finally — connection leak",
            "Variable naming: user_age, userEmail, UserName — inconsistent",
        ],
        "expect": {
            "p_min": 2,
            "p_cats": ["security"],
            "p_hallu_max": 20,
            "r_hallu_kw": [],
            "j_dec": "APPROVED",
            "j_p_min": 15,
            "j_p_max": 30,
            "j_r_min": 15,
            "j_r_max": 30,
        },
    },
]


# ── Helpers ───────────────────────────────────────────────────────────
TIMEOUT = 600
LOG_TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log(msg):
    timestamp_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{timestamp_str}] {msg}", flush=True)


def build_ctx(gt):
    """Build P user context — same format as prj_ground_truth_test.py."""
    ftext = "\n".join(f"[text] {f}" for f in gt["facts"])
    return (
        f"=== USER TURN ===\n{gt['user']}\n\n=== THINKING ===\n{gt['think']}\n\n"
        f"=== RESPONSE ===\n{gt['text']}\n\n=== EXTRACTED FACTS ===\n{ftext}"
    ), ftext


def _current_mode() -> Optional[str]:
    """Read current mode from env file."""
    env_path = "/opt/ai_data/scripts/current-mode-inference.env"
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("MODE="):
                    return line.split("=", 1)[1]
    except Exception:
        pass
    return None


def _health_ok(timeout: int = 600) -> bool:
    """Poll :8081/health until 200 or timeout."""
    url = f"http://127.0.0.1:{MODEL_REGISTRY['proposer']['port']}/health"
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(5)
    return False


# Swap: P=30B Q4_K_M (CTX=4096, q8_0 cache), R=14B Q4_K_M (fast binary decisions), J=N14B Q6_K
_SWAP_OVERRIDE = {
    "review-p": {
        "MODEL_FILE": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        "CTX_SIZE": "4096",
        "CACHE_RAM": "512",
        "MLOCK": "0",
        "EVICT_ROOM": "14000",
        "THREADS": "4",
        "THREADS_BATCH": "4",
        "CACHE_TYPE_K": "q8_0",
        "CACHE_TYPE_V": "q8_0",
    },
    "review-r": {
        "MODEL_FILE": "qwen2.5-coder-14b-instruct-q4_k_m.gguf",
        "CTX_SIZE": "8192",
        "CACHE_RAM": "512",
        "MLOCK": "0",
        "EVICT_ROOM": "13000",
        "THREADS": "4",
        "THREADS_BATCH": "4",
    },
    "review-j": {
        "MODEL_FILE": "NextCoder-14B-Q6_K.gguf",
        "CTX_SIZE": "6144",
        "CACHE_RAM": "512",
        "MLOCK": "0",
        "EVICT_ROOM": "13000",
        "THREADS": "4",
        "THREADS_BATCH": "4",
    },
}


def _check_swap_skip(cur: str, target: str) -> bool:
    """Same MODEL_FILE → skip restart. E.g. Round1 P→Round2 P (both 14B)."""
    if cur and target and cur != target:
        cur_f = _SWAP_OVERRIDE.get(cur, {}).get("MODEL_FILE")
        tgt_f = _SWAP_OVERRIDE.get(target, {}).get("MODEL_FILE")
        if cur_f and tgt_f and cur_f == tgt_f:
            log(f"  [swap] {cur} → {target}: same model ({cur_f}), restart skipped")
            return True
    return False


def swap_inference(mode: str, max_wait: int = 1200) -> bool:
    """Swap inference model. Skips restart when same MODEL_FILE as current mode."""
    cur = _current_mode()
    skip_restart = _check_swap_skip(cur, mode)
    if not skip_restart:
        if cur == mode:
            log(f"  [swap] already {mode} — checking health")
            if _health_ok(30):
                log("  [swap] healthy, skip swap")
                return True
            log("  [swap] unhealthy — will force restart")
        log(f"  [swap] inference -> {mode}")
    try:
        from lib.pod_manager import _write_mode_env as _wenv

        _wenv(mode, 8081)
        env_file = "/opt/ai_data/scripts/current-mode-inference.env"
        ov = _SWAP_OVERRIDE.get(mode)
        if ov:
            with open(env_file) as f:
                lines = f.readlines()
            with open(env_file, "w") as f:
                seen = set()
                for line in lines:
                    key = line.split("=", 1)[0]
                    if key in ov and key not in seen:
                        f.write(f"{key}={ov[key]}\n")
                        seen.add(key)
                    elif key not in ov:
                        f.write(line)
                for k, v in ov.items():
                    if k not in seen:
                        f.write(f"{k}={v}\n")
    except Exception as e:
        log(f"  [swap] env write failed ({e})")
        env = "/opt/ai_data/scripts/current-mode-inference.env"
        with open(env, "w") as f:
            f.write(f"MODE={mode}")
    if skip_restart:
        return True
    _podman_stop_inference()
    _podman_start_inference()
    ok = _health_ok(max_wait)
    if ok:
        log(f"  [swap] :8081 ready for {mode}")
    else:
        log(f"  [swap] :8081 TIMEOUT after {max_wait}s for {mode}")
    return ok


def call_llm_json(system: str, user: str, model: str = "proposer", max_tokens: int = 1024) -> dict:
    """Call inference with JSON mode. `model` must be a MODEL_REGISTRY key."""
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
            max_tokens=max_tokens,
            temperature=0.1,
            timeout=TIMEOUT,
            json_mode=True,
            return_meta=True,
        )
        raw = meta.get("content", "")
        # JSON parse (handle ```json fences like night.py call_model)
        if isinstance(raw, str):
            import re

            raw = raw.strip()
            m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
            if m:
                raw = m.group(1).strip()
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as e:
                # Try finding last }
                end = raw.rfind("}")
                if end > 0 and "Extra data" in str(e):
                    parsed = json.loads(raw[: end + 1])
                else:
                    parsed = None
        else:
            parsed = raw
        ok = parsed is not None
        return {
            "ok": ok,
            "parsed": parsed,
            "raw": str(raw)[:400],
            "elapsed_s": round(time.monotonic() - t0, 1),
            "error": "",
        }
    except Exception as e:
        return {
            "ok": False,
            "parsed": None,
            "raw": str(e)[:400],
            "elapsed_s": round(time.monotonic() - t0, 1),
            "error": str(e)[:200],
        }


# ── Scoring (from prj_ground_truth_test.py) ──────────────────────────
def score_p(findings, exp, source_text, accepted_ids=None):
    """Score P. Hallucination = R-rejected (accepted_ids에 없음)."""
    n = len(findings) if isinstance(findings, list) else 0
    d = {"n": n}

    if exp["p_min"] == 0 and n == 0:
        return 100, "empty(OK)", d
    if exp["p_min"] > 0 and n == 0:
        return 0, f"empty(exp={exp['p_min']})", d
    if exp["p_min"] == 0 and n > 0:
        return 10, f"false_pos({n})", d
    cnt_ok = n >= exp["p_min"]

    cats = set((f.get("type") or f.get("category") or "").lower() for f in findings)
    cat_hits = sum(1 for c in exp["p_cats"] if c in cats)
    cat_ok = cat_hits == len(exp["p_cats"])
    d["cats"] = sorted(cats)

    # Hallucination: R-rejected = hallucination (MiniCheck 제거)
    h = 0
    if accepted_ids is not None:
        for f in findings:
            if f.get("id", "") not in accepted_ids:
                h += 1
    hpct = h / n * 100 if n > 0 else 0
    h_ok = hpct <= exp["p_hallu_max"] if exp["p_hallu_max"] > 0 else (h == 0)
    d["hallu"] = f"{h}/{n}"

    pts = (20 if cnt_ok else 0) + (30 if cat_ok else 0) + (50 if h_ok else 0)
    det = f"{n}f cats={','.join(d['cats'])} hallu={h}/{n}"
    return pts, det, d


def score_r(verdicts, findings, exp):
    if not isinstance(verdicts, list):
        return 0, "no_list", {}
    if not findings:
        return 100, "no_findings", {}
    acc = [v for v in verdicts if v.get("verdict", "").lower() == "accept"]
    rej = [v for v in verdicts if v.get("verdict", "").lower() == "reject"]
    rej_ids = set(v.get("id", "") for v in rej)

    h_kw = exp.get("r_hallu_kw", [])
    h_rej_ok = True
    if h_kw:
        for f in findings:
            blob = (
                f.get("description", "")
                + " "
                + f.get("evidence", "")
                + " "
                + f.get("rationale", "")
            ).lower()
            if any(k.lower() in blob for k in h_kw):
                if f.get("id", "") not in rej_ids:
                    h_rej_ok = False
    ratio = len(acc) / len(verdicts) if verdicts else 1
    pts = (60 if h_rej_ok else 0) + (40 if ratio >= 0.5 else 0)
    det = f"{len(acc)}A/{len(rej)}R hallu_rej={'OK' if h_rej_ok else 'FAIL'}"
    return pts, det, {"acc": len(acc), "rej": len(rej)}


def score_j(res, exp):
    if not isinstance(res, dict):
        return 0, "no_dict", {}
    p = res.get("P_score", -1)
    r = res.get("R_score", -1)
    d = (res.get("decision") or "").upper()

    dec_ok = d == exp["j_dec"]
    p_ok = exp["j_p_min"] <= p <= exp["j_p_max"]
    r_ok = exp["j_r_min"] <= r <= exp["j_r_max"]

    pts = (40 if dec_ok else 0) + (30 if p_ok else 0) + (30 if r_ok else 0)
    det = f"dec={d}({'OK' if dec_ok else 'WRONG'}) P={p} R={r}"
    return pts, det, {"dec": d, "P": p, "R": r}


# ── Role runners ──────────────────────────────────────────────────────
def run_role(
    role: str,
    system_prompt: str,
    user_inputs: List[str],
    model: str = "proposer",
    max_tokens: int = 1024,
) -> List[dict]:
    """Run one role across all GT cases. Batch: same model for all cases."""
    results = []
    for i, gt in enumerate(GT):
        ctx = user_inputs[i]
        resp = call_llm_json(system_prompt, ctx, model=model, max_tokens=max_tokens)
        parsed = resp["parsed"] if resp["ok"] else {}
        results.append(
            {"resp": resp, "parsed": parsed, "elapsed": resp["elapsed_s"], "ok": resp["ok"]}
        )
        log(f"    [{i + 1}] {gt['id']}: {resp['elapsed_s']}s {'OK' if resp['ok'] else 'FAIL'}")
    return results


def run_round():
    """Full P→R→J round. R = hallucination detector for P scoring."""
    print(f"\n{'=' * 70}")
    print("  Night P-R-J Ground Truth Evaluation (v2)")
    print("  R = hallucination detector (MiniCheck 제거)")
    print(f"{'=' * 70}")

    # Phase P — 30B
    print(f"\n{'─' * 70}")
    print("  [P] 30B Q4_K_M — Proposer")
    print(f"{'─' * 70}")
    if not swap_inference(P_MODE, max_wait=1200):
        print("  ERROR: P swap failed — aborting")
        return None
    p_ctxs = []
    for gt in GT:
        ctx, _ = build_ctx(gt)
        p_ctxs.append(f"## Audit Summary\n{ctx}")
    p_results = run_role("P", SYSTEM_PROPOSER, p_ctxs, model="proposer", max_tokens=2048)

    # Phase R — 14B (hallucination detector)
    print(f"\n{'─' * 70}")
    print("  [R] 14B Q4_K_M — Refuter & Hallucination Detector")
    print(f"{'─' * 70}")
    if not swap_inference(R_MODE, max_wait=600):
        print("  ERROR: R swap failed — aborting")
        return None
    r_ctxs = []
    for i, gt in enumerate(GT):
        p = p_results[i]
        pf = p["parsed"].get("findings", []) if p["parsed"] else []
        r_ctxs.append(json.dumps(pf, indent=2, ensure_ascii=False) if pf else "No findings.")
    r_results = run_role("R", SYSTEM_REFUTER, r_ctxs, model="reflector", max_tokens=1024)

    # Score P + R
    print(f"\n{'─' * 70}")
    print("  SCORES (P+R)")
    print(f"{'─' * 70}")
    scores = []
    for i, gt in enumerate(GT):
        p = p_results[i]
        r = r_results[i]
        pf = p["parsed"].get("findings", []) if p["parsed"] else []
        rv = r["parsed"].get("verdicts", []) if r["parsed"] else []
        acc_ids = set(v.get("id", "") for v in rv if v.get("verdict", "").lower() == "accept")

        ps, pd, _ = score_p(pf, gt["expect"], gt["text"], accepted_ids=acc_ids if rv else None)
        rs, rd, _ = score_r(rv, pf, gt["expect"])
        scores.append(
            {
                "case": gt["id"],
                "title": gt["title"],
                "P": ps,
                "p_detail": pd,
                "p_elapsed": p["elapsed"],
                "R": rs,
                "r_detail": rd,
                "r_elapsed": r["elapsed"],
            }
        )
        pbar = "█" * int(ps / 10) + "░" * (10 - int(ps / 10))
        rbar = "█" * int(rs / 10) + "░" * (10 - int(rs / 10))
        print(f"  [{i + 1}] {gt['title']}")
        print(f"    P: {ps:>3}/100 {pbar} ({pd}) [{p['elapsed']:.0f}s]")
        print(f"    R: {rs:>3}/100 {rbar} ({rd}) [{r['elapsed']:.0f}s]")

    p_avg = sum(s["P"] for s in scores) / len(scores)
    r_avg = sum(s["R"] for s in scores) / len(scores)
    print(f"\n  P={p_avg:.0f} R={r_avg:.0f}")

    # Phase J — 14B
    print(f"\n{'─' * 70}")
    print("  [J] N14B Q6_K — Judge")
    print(f"{'─' * 70}")
    if not swap_inference(J_MODE, max_wait=600):
        print("  ERROR: J swap failed — aborting")
        return None
    j_ctxs = []
    for i, gt in enumerate(GT):
        p = p_results[i]
        r = r_results[i]
        pf = p["parsed"].get("findings", []) if p["parsed"] else []
        rv = r["parsed"].get("verdicts", []) if r["parsed"] else []
        ctx = (
            f"## Audit Summary\n{gt['text']}\n\n"
            f"## P Findings\n{json.dumps(pf, indent=2, ensure_ascii=False)}\n\n"
            f"## R Verdicts\n{json.dumps(rv, indent=2, ensure_ascii=False)}"
        )
        j_ctxs.append(ctx)
    j_results = run_role("J", SYSTEM_JUDGE, j_ctxs, model="judge", max_tokens=512)

    # Score J
    for i, gt in enumerate(GT):
        j = j_results[i]
        jp = j["parsed"] if j["parsed"] else {}
        js, jd, _ = score_j(jp, gt["expect"])
        scores[i]["J"] = js
        scores[i]["j_detail"] = jd
        scores[i]["j_elapsed"] = j["elapsed"]
        jbar = "█" * int(js / 10) + "░" * (10 - int(js / 10))
        print(f"    J: {js:>3}/100 {jbar} ({jd}) [{j['elapsed']:.0f}s]")

    j_avg = sum(s["J"] for s in scores) / len(scores)
    total = round((p_avg + r_avg + j_avg) / 3, 1)
    print(f"\n  {'─' * 50}")
    print(f"  종합: P={p_avg:.0f} R={r_avg:.0f} J={j_avg:.0f} 평균={total}")

    save_results(scores, p_results, r_results, j_results)
    return scores, p_results, r_results, j_results


def save_results(scores: list, p_res, r_res, j_res):
    p_avg = sum(s["P"] for s in scores) / len(scores)
    out = {
        "round": "P=30B Q4_K_M → R=14B Q4_K_M → J=N14B Q6_K",
        "timestamp": LOG_TIMESTAMP,
        "scores": scores,
        "p_avg": p_avg,
    }
    # Add R/J if available (full round)
    if scores and "R" in scores[0]:
        out["r_avg"] = sum(s["R"] for s in scores) / len(scores)
        out["j_avg"] = sum(s["J"] for s in scores) / len(scores)
        out["total"] = round(
            (
                sum(s["P"] for s in scores)
                + sum(s["R"] for s in scores)
                + sum(s["J"] for s in scores)
            )
            / (len(scores) * 3),
            1,
        )
    fpath = f"/tmp/night_prj_eval_P30B_R14B_JN14B_{LOG_TIMESTAMP}.json"
    with open(fpath, "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log(f"  [save] {fpath}")


# ── Main ──────────────────────────────────────────────────────────────
def main():
    TEST = test_setup("test_night_prj_eval", "Night P-R-J Ground Truth Evaluation")
    print("=" * 70)
    print("  Night P-R-J Ground Truth Evaluation")
    print(f"  {len(GT)} scenarios, night.py v4.0 models")
    print("  P=30B Q4_K_M | R=hallucination_detector(14B) | J=N14B Q6_K")
    print("=" * 70)

    result = run_round()
    if result is None:
        print("  Evaluation failed — aborting")
        sys.exit(1)
    scores, p_results, r_results, j_results = result

    # Restore inference
    print("\n  Restoring inference to day mode...")
    try:
        from lib.pod_manager import _write_mode_env as _wenv

        _wenv("day", 8082)
    except Exception:
        with open("/opt/ai_data/scripts/current-mode-inference.env", "w") as f:
            f.write("MODE=day")
    _podman_stop_inference()
    _podman_start_inference()
    print("  Done.")
    test_complete("night PRJ evaluation done")


if __name__ == "__main__":
    main()
