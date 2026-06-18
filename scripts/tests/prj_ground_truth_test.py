#!/usr/bin/env python3
# Status: experimental
# Path: none — P/R/J ground-truth evaluation (Pod B swap, 4 cores dedicated)
"""Ground truth 기반 P→R→J day 분류 3모델 정밀 평가.
Pod B swap 방식: 각 모델마다 Pod B stop → model load → test → stop → swap.
4코어 전용 할당. Pod A 미사용 (완전 중단).
P(Mistral) → R(Qwen Instruct) → J(Llama 3.1).
종합 점수: hallucination(MiniCheck), 정확도, 일관성."""
import json, os, subprocess, sys, time, urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm_client import MODEL_REGISTRY
from lib.test_common import test_setup, test_heartbeat, test_complete, log, call_llm, parse_llm_json
from minicheck.minicheck import MiniCheck

# ── MiniCheck ───────────────────────────────────────────────────────
NLI = MiniCheck(model_name="flan-t5-large", cache_dir="/opt/ai_data/models")
MC_THRESHOLD = 0.3

def mc_verify(evidence: str, source: str) -> bool:
    if not evidence or not source:
        return False
    label, prob, _, _ = NLI.score(docs=[source], claims=[evidence])
    return bool(label[0] == 1 and prob[0] >= MC_THRESHOLD)


# ── Models ──────────────────────────────────────────────────────────
P_MODEL = "Mistral-7B-Instruct-v0.3-Q8_0.gguf"
R_MODEL = "Qwen2.5-7B-Instruct-Q8_0.gguf"
J_MODEL = "Meta-Llama-3.1-8B-Instruct-Q8_0.gguf"
P_NAME = "Mistral 7B Instruct"
R_NAME = "Qwen 2.5 7B Instruct"
J_NAME = "Llama 3.1 8B Instruct"


# ── Ground Truth: 5 scenarios ───────────────────────────────────────
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
            "p_min": 1, "p_cats": ["performance"], "p_hallu_max": 20,
            "r_hallu_kw": [],
            "j_dec": "APPROVED", "j_p_min": 15, "j_p_max": 30, "j_r_min": 15, "j_r_max": 30,
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
            "p_min": 1, "p_cats": ["security"], "p_hallu_max": 20,
            "r_hallu_kw": [],
            "j_dec": "APPROVED", "j_p_min": 15, "j_p_max": 30, "j_r_min": 15, "j_r_max": 30,
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
            "p_min": 0, "p_cats": [], "p_hallu_max": 0,
            "r_hallu_kw": [],
            "j_dec": "APPROVED", "j_p_min": 0, "j_p_max": 10, "j_r_min": 0, "j_r_max": 10,
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
            "p_min": 1, "p_cats": ["quality"], "p_hallu_max": 30,
            "r_hallu_kw": ["crash", "OOM", "out of memory"],
            "j_dec": "APPROVED", "j_p_min": 5, "j_p_max": 30, "j_r_min": 5, "j_r_max": 30,
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
            "p_min": 2, "p_cats": ["security"], "p_hallu_max": 20,
            "r_hallu_kw": [],
            "j_dec": "APPROVED", "j_p_min": 15, "j_p_max": 30, "j_r_min": 15, "j_r_max": 30,
        },
    },
]


# ── Prompts (mirrors classify.py) ───────────────────────────────────
PROMPTS = {
    "P": """You are a code review assistant. Examine the turn and extracted facts below.
Generate findings about potential issues, bugs, or improvements.

CRITICAL RULES:
- Each finding MUST cite specific evidence from the extracted facts.
- If no clear issue exists, return an empty findings list.
- Do NOT fabricate code, file paths, or function names.
- Do NOT guess. If uncertain, leave it out.
- Maximum 20 findings per turn.

Return JSON:
{"findings": [{"id":"D001","severity":"critical|high|medium|low","category":"bug|security|data_loss|performance|quality","description":"...","evidence":"..."}]}""",

    "R": """You are a verdict reviewer. For each finding proposed by the reviewer,
decide ACCEPT or REJECT based ONLY on whether the evidence supports the finding.

CRITICAL RULES:
- ACCEPT: evidence clearly supports the finding.
- REJECT: evidence is weak, missing, or contradicts the finding.
- REJECT: duplicate of another finding.

Return JSON:
{"verdicts": [{"id":"D001","verdict":"accept|reject","reason":"..."}]}""",

    "J": """You are a scoring judge. Review the findings and verdicts.
CRITICAL RULES:
- P_score (0-30) = quality of findings (correctness + coverage + precision)
- R_score (0-30) = quality of verdicts (accuracy + efficiency)
- decision = APPROVED if majority accepted, REJECT otherwise

Return JSON:
{"P_score":0-30,"R_score":0-30,"decision":"APPROVED|REJECT","consensus_score":0-100,"approved":["D001"],"rejected":[],"report":{"summary":"...","top_issues":[]}}""",
}


# ── Helpers ─────────────────────────────────────────────────────────
TIMEOUT = 300

def log(msg):
    print(f"  {msg}", flush=True)


def restart_pod_b(model_file: str, model_name: str = "test") -> bool:
    """Swap Pod B to new model on :8082. 4 cores dedicated, no Pod A."""
    ENV = "/opt/ai_data/scripts/current-mode-pod-b.env"
    with open(ENV, "w") as f:
        escaped_name = model_name.replace('"', '\\"')
        f.write(f"MODE=day\nMODEL_NAME=\"{escaped_name}\"\nPORT=8082\nMODEL_FILE={model_file}\n"
                f"CTX_SIZE=8192\nTHREADS=4\nTHREADS_BATCH=4\nCACHE_RAM=1024\n")
    log("Stopping Pod B...")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b"],
                   capture_output=True, timeout=60)
    time.sleep(3)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b"],
                   capture_output=True, timeout=10)
    log(f"Starting Pod B with {model_file}...")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b"],
                   capture_output=True, timeout=60)
    for i in range(300):
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request(f"http://127.0.0.1:{MODEL_REGISTRY['extractor']['port']}/health"), timeout=5)
            if resp.status == 200:
                log(f"Ready in {i+1}s")
                time.sleep(5)
                return True
        except Exception:
            pass
        if i > 0 and i % 30 == 0:
            log(f"... {i+1}s")
        time.sleep(2)
    return False


def call_llm_json(system: str, user: str) -> dict:
    """Call Pod B :8082 with JSON mode. Returns {'ok', 'parsed', 'elapsed_s', 'error'}."""
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            model="extractor", max_tokens=512, temperature=0.1,
            timeout=TIMEOUT, json_mode=True, return_meta=True,
        )
        raw = meta.get("content", "")
        parsed = parse_llm_json(raw)
        ok = parsed is not None
        return {"ok": ok, "parsed": parsed, "raw": raw[:400],
                "elapsed_s": round(time.monotonic() - t0, 1), "error": ""}
    except Exception as e:
        return {"ok": False, "parsed": None, "raw": str(e)[:400],
                "elapsed_s": round(time.monotonic() - t0, 1), "error": str(e)[:200]}


def build_ctx(gt):
    """Build P user context."""
    ftext = "\n".join(f"[text] {f}" for f in gt["facts"])
    return (f"=== USER TURN ===\n{gt['user']}\n\n=== THINKING ===\n{gt['think']}\n\n"
            f"=== RESPONSE ===\n{gt['text']}\n\n=== EXTRACTED FACTS ===\n{ftext}")


# ── Scoring ─────────────────────────────────────────────────────────
def score_p(findings, exp, ftext):
    n = len(findings) if isinstance(findings, list) else 0
    d = {"n": n}

    # count
    if exp["p_min"] == 0 and n == 0:
        return 100, "empty(OK)", d
    if exp["p_min"] > 0 and n == 0:
        return 0, f"empty(exp={exp['p_min']})", d
    if exp["p_min"] == 0 and n > 0:
        return 10, f"false_pos({n})", d
    cnt_ok = n >= exp["p_min"]

    # category
    cats = set((f.get("category") or "").lower() for f in findings)
    cat_hits = sum(1 for c in exp["p_cats"] if c in cats)
    cat_ok = cat_hits == len(exp["p_cats"])
    d["cats"] = sorted(cats)

    # hallucination (MiniCheck)
    h = 0
    for f in findings:
        ev = f.get("evidence", "")
        if ev and not mc_verify(ev, ftext):
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
            blob = (f.get("description","") + " " + f.get("evidence","")).lower()
            if any(k in blob for k in h_kw):
                if f.get("id","") not in rej_ids:
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


# ── Role runners ────────────────────────────────────────────────────
def run_p():
    """P(Mistral): findings for all GT cases."""
    ftexts = []
    for gt in GT:
        ftexts.append("\n".join(f"[text] {f}" for f in gt["facts"]))
    log(f"P({P_NAME}): {len(GT)} cases")
    results = []
    for i, gt in enumerate(GT):
        ctx = build_ctx(gt)
        resp = call_llm_json(PROMPTS["P"], ctx)
        findings = resp["parsed"].get("findings", []) if resp["parsed"] and isinstance(resp["parsed"], dict) else []
        if not isinstance(findings, list):
            findings = []
        pts, det, diag = score_p(findings, gt["expect"], ftexts[i])
        log(f"  [{i+1}] {gt['id']}: {pts}/100 ({det}) [{resp['elapsed_s']}s]")
        if pts < 60 and resp["raw"]:
            log(f"       raw: {resp['raw'][:150]}")
        results.append({"findings": findings, "score": pts, "detail": det, "diag": diag, "elapsed": resp["elapsed_s"]})
    return results


def run_r(p_results):
    """R(Qwen): verdicts on P's findings."""
    log(f"R({R_NAME}): {len(GT)} cases")
    results = []
    for i, gt in enumerate(GT):
        findings = p_results[i]["findings"]
        if not findings:
            pts, det = 100, "no_findings"
            log(f"  [{i+1}] {gt['id']}: {pts}/100 ({det})")
            results.append({"verdicts": [], "score": pts, "detail": det, "diag": {}, "elapsed": 0})
            continue
        ctx = json.dumps(findings, ensure_ascii=False, indent=2)[:4000]
        resp = call_llm_json(PROMPTS["R"], f"Review these findings:\n{ctx}")
        verdicts = resp["parsed"].get("verdicts", []) if resp["parsed"] and isinstance(resp["parsed"], dict) else []
        if not isinstance(verdicts, list):
            verdicts = []
        pts, det, diag = score_r(verdicts, findings, gt["expect"])
        log(f"  [{i+1}] {gt['id']}: {pts}/100 ({det}) [{resp['elapsed_s']}s]")
        if pts < 60 and resp["raw"]:
            log(f"       raw: {resp['raw'][:150]}")
        results.append({"verdicts": verdicts, "score": pts, "detail": det, "diag": diag, "elapsed": resp["elapsed_s"]})
    return results


def run_j(p_results, r_results):
    """J(Llama): final score from P+R."""
    log(f"J({J_NAME}): {len(GT)} cases")
    results = []
    for i, gt in enumerate(GT):
        findings = p_results[i]["findings"]
        verdicts = r_results[i]["verdicts"]
        if not findings or not verdicts:
            pts, det = 100, "no_need"
            log(f"  [{i+1}] {gt['id']}: {pts}/100 ({det})")
            results.append({"result": {}, "score": pts, "detail": det, "diag": {}, "elapsed": 0})
            continue
        ctx = (f"Findings:\n{json.dumps(findings, ensure_ascii=False, indent=2)[:2000]}\n\n"
               f"Verdicts:\n{json.dumps(verdicts, ensure_ascii=False, indent=2)[:2000]}")
        resp = call_llm_json(PROMPTS["J"], ctx)
        pts, det, diag = score_j(resp["parsed"], gt["expect"])
        log(f"  [{i+1}] {gt['id']}: {pts}/100 ({det}) [{resp['elapsed_s']}s]")
        if pts < 60 and resp["raw"]:
            log(f"       raw: {resp['raw'][:150]}")
        results.append({"result": resp["parsed"], "score": pts, "detail": det, "diag": diag, "elapsed": resp["elapsed_s"]})
    return results


def restore_default():
    """Restore Pod B to 7B extractor (normal day mode)."""
    log("Restoring Pod B to 7B extractor (day mode)...")
    with open("/opt/ai_data/scripts/current-mode-pod-b.env", "w") as f:
        f.write("MODE=day\nMODEL_NAME=extractor\nPORT=8082\n"
                "MODEL_FILE=Qwen2.5-Coder-7B-Instruct.Q8_0.gguf\n"
                "CTX_SIZE=8192\nTHREADS=2\nTHREADS_BATCH=2\nCACHE_RAM=512\n")
    subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b"],
                   capture_output=True, timeout=120)


# ── Main ────────────────────────────────────────────────────────────
def main():
    TEST = test_setup("prj_ground_truth", "P/R/J ground-truth evaluation (Pod B swap, 4 cores)")
    print("=" * 70)
    print("  Ground Truth Evaluation — P→R→J Day Classification")
    print(f"  {len(GT)} scenarios, 3 models")
    print(f"  P({P_NAME}) → R({R_NAME}) → J({J_NAME})")
    print(f"  Hallucination: MiniCheck(flan-t5-large, threshold=0.3)")
    print("=" * 70)

    ok_to_continue = True

    # Phase P
    print(f"\n{'─'*70}\n  [P] {P_NAME} (Proposer)\n{'─'*70}")
    if not os.path.exists(f"/opt/ai_data/models/gguf/{P_MODEL}"):
        log(f"SKIP: {P_MODEL} not found")
        ok_to_continue = False
    if ok_to_continue and restart_pod_b(P_MODEL, P_NAME):
        p_res = run_p()
    else:
        p_res = [{"findings": []} for _ in GT]

    # Phase R
    print(f"\n{'─'*70}\n  [R] {R_NAME} (Reflector)\n{'─'*70}")
    if not os.path.exists(f"/opt/ai_data/models/gguf/{R_MODEL}"):
        log(f"SKIP: {R_MODEL} not found")
        r_res = [{"verdicts": []} for _ in GT]
    elif ok_to_continue and restart_pod_b(R_MODEL, R_NAME):
        r_res = run_r(p_res)
    else:
        r_res = [{"verdicts": []} for _ in GT]

    # Phase J
    print(f"\n{'─'*70}\n  [J] {J_NAME} (Judge)\n{'─'*70}")
    if not os.path.exists(f"/opt/ai_data/models/gguf/{J_MODEL}"):
        log(f"SKIP: {J_MODEL} not found")
        judge_results = [{"result": {}} for _ in GT]
    elif ok_to_continue and restart_pod_b(J_MODEL, J_NAME):
        judge_results = run_j(p_res, r_res)
    else:
        judge_results = [{"result": {}} for _ in GT]

    # ── Report ─────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("  REPORT — Pod B swap (4 cores dedicated)")
    print(f"{'='*70}")

    # Role summary
    for label, results in [("P (Proposer)", p_res), ("R (Reflector)", r_res), ("J (Judge)", judge_results)]:
        avg = sum(r["score"] for r in results) / len(results)
        bar = "█" * int(avg / 10) + "░" * (10 - int(avg / 10))
        print(f"\n  {label}: {avg:.0f}% {bar}")

    # Case detail table
    print(f"\n{'─'*70}")
    for i, gt in enumerate(GT):
        p = p_res[i]; r = r_res[i]; j = judge_results[i]
        print(f"\n  [{i+1}] {gt['title']}")
        print(f"    P: {p['score']}/100 ({p['detail']}) [{p.get('elapsed',0):.0f}s]")
        print(f"    R: {r['score']}/100 ({r['detail']}) [{r.get('elapsed',0):.0f}s]")
        print(f"    J: {j['score']}/100 ({j['detail']}) [{j.get('elapsed',0):.0f}s]")

    # Restore Pod B to day mode (7B extractor)
    if ok_to_continue:
        print(f"\n  Restoring Pod B to 7B extractor (day mode)...")
        restore_default()
        log("Done")
    else:
        log("Skipped restore (some models missing)")

    print(f"{'='*70}")
    test_complete("PRJ GT evaluation done")


if __name__ == "__main__":
    main()
