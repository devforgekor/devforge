#!/usr/bin/env python3
# Status: experimental
# Path: none — 14B Q8_0 단일 extract + MiniCheck verify 속도/정확도 테스트
"""14B Q8_0 단일 extract + MiniCheck hallucination 검증.
Pod B에 14B 올려서 MCP context 포함 full extract 1회 + MiniCheck verify.
이 구조가 day mode에서 실전 가능한지 판단."""
import json, os, subprocess, sys, time, urllib.request

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.environ["TOKENIZERS_PARALLELISM"] = "false"

from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm
from minicheck.minicheck import MiniCheck

NLI = MiniCheck(model_name="flan-t5-large", cache_dir="/opt/ai_data/models")
MC_THRESHOLD = 0.3

# ── 14B extract prompt (single pass, full context) ─────────────────
EXTRACT_PROMPT = """You are a code review extractor. Analyze the code review turn and all context below.
Extract ALL potential issues, bugs, security problems, and improvements.

RULES:
- Each finding MUST cite specific evidence from the provided text.
- If no clear issue exists, return empty findings list.
- Consider: security, performance, data_loss, bugs, naming, best practices
- Maximum 20 findings per turn.

Return JSON:
{"findings": [{"id":"D001","severity":"critical|high|medium|low","category":"bug|security|data_loss|performance|quality","description":"...","evidence":"..."}]}"""

# ── 5 test cases (GT scenarios, same as before) ────────────────────
GT = [
    {"id":"perf_bug","title":"성능 버그 (DB connection pool 누수)",
     "user":"로그인 API가 3초나 걸리는데 원인이 뭘까?",
     "text":"/api/v1/auth/login 확인 결과 auth_routes.py login()이 매 요청마다 새 DB 세션을 염. AsyncSession이지만 연결 풀링이 전혀 안 됨. 이것이 3초 응답의 주 원인.",
     "facts":["login API response time is 3 seconds","auth_routes.py login() opens a new async session per request — no pooling","DB connection pool exhaustion이 근본 원인"],
     "mcp_facts":["Repository: devforge/server, Branch: main, Last deploy: 2026-06-08","PostgreSQL 16, pg_trgm enabled, JSONB available"]},
    {"id":"security_leak","title":"보안 이슈 (API 키 로깅 노출)",
     "user":"에러 로그에서 API 키가 발견됐어. 어떻게 해야 할까?",
     "text":"Auth 실패 시 stack trace + 요청 파라미터가 /var/log/app/error.log에 기록됨. Authorization 헤더 API key도 함께 로깅. 로그 보존 90일.",
     "facts":["API keys logged to /var/log/app/error.log on auth failure","Log retention: 90 days","Logs accessible by all developers"],
     "mcp_facts":["Log rotation: /etc/logrotate.d/app 7일 주기","GDPR compliance required"]},
    {"id":"no_issue","title":"정상 코드 (이슈 없음)",
     "user":"CI 파이프라인 통과했어. 배포해도 될까?",
     "text":"CI 파이프라인 2분 완료. 모든 테스트 통과. 코드 커버리지 87%.",
     "facts":["CI pipeline completes in 2 minutes","All tests pass","Code coverage is 87%"],
     "mcp_facts":["Deployment: Caddy reverse proxy, blue-green"]},
    {"id":"hallu_trap","title":"할루시네이션 트랩",
     "user":"서버 메모리가 부족한 것 같아. OOM이 날까 걱정이야.",
     "text":"서버 상태: 메모리 22GB 중 12GB 사용, 10GB 여유. 스왑 8GB 중 508MB 사용. 부하 2.55/2.46/4.02. OOM 위험 낮음. 디스크 92% — 모델 파일 정리 필요.",
     "facts":["Memory: 22Gi total, 12Gi used, 10Gi available","Swap: 8Gi total, 508Mi used","Load: 1min=2.55, 5min=2.46, 15min=4.02","/opt/ai_data disk at 92%"],
     "mcp_facts":["OOM score: 500 for LLM processes","cgroups split enabled"]},
    {"id":"mixed","title":"복합 이슈 (스타일+보안+누수)",
     "user":"코드 리뷰 좀 해줘. naming 컨벤션하고 보안 관련해서.",
     "text":"분석 결과:\n1. user_table/userAccount 혼용\n2. users.password plaintext — 해싱 없음\n3. conn.close() try-finally 누락\n4. user_age, userEmail, UserName 혼용",
     "facts":["user_table and userAccount naming mixed — snake_case and camelCase","users.password column plaintext — no password hashing","conn.close() missing in try-finally — connection leak","Variable naming inconsistent"],
     "mcp_facts":["Code style: PEP 8","Security policy: plaintext passwords prohibited"]},
]

# ── Ground Truth expectations ──────────────────────────────────────
EXPECT = {
    "perf_bug":     {"cats":["performance"], "hallu_max":20, "min":1},
    "security_leak":{"cats":["security"], "hallu_max":20, "min":1},
    "no_issue":     {"cats":[], "hallu_max":0, "min":0},
    "hallu_trap":   {"cats":["quality"], "hallu_max":30, "min":1},
    "mixed":        {"cats":["security"], "hallu_max":20, "min":2},
}


def build_context(gt):
    parts = []
    parts.append(f"=== USER TURN ===\n{gt['user']}\n\n=== RESPONSE ===\n{gt['text']}")
    parts.append("\n=== EXTRACTED FACTS ===")
    for f in gt["facts"]:
        parts.append(f"[fact] {f}")
    if gt["mcp_facts"]:
        parts.append("\n=== MCP CONTEXT (Global Project Info) ===")
        for m in gt["mcp_facts"]:
            parts.append(f"[mcp] {m}")
    return "\n".join(parts)


def mc_verify(evidence: str, source: str) -> bool:
    if not evidence or not source:
        return False
    label, prob, _, _ = NLI.score(docs=[source], claims=[evidence])
    return bool(label[0] == 1 and prob[0] >= MC_THRESHOLD)


def restart_pod_b():
    env_content = (
        "MODE=day\n"
        "MODEL_NAME=\"Qwen 2.5 Coder 14B\"\n"
        "PORT=8082\n"
        "MODEL_FILE=Qwen2.5-Coder-14B-Instruct.Q8_0.gguf\n"
        "CTX_SIZE=16384\n"
        "THREADS=4\n"
        "THREADS_BATCH=4\n"
        "CACHE_RAM=2048\n"
    )
    with open("/opt/ai_data/scripts/current-mode-pod-b.env", "w") as f:
        f.write(env_content)
    print("  Env written. Stopping Pod B...", flush=True)
    subprocess.run(["systemctl","--user","stop","container-devforge-pod-b"],
                   capture_output=True, timeout=60)
    time.sleep(3)
    subprocess.run(["systemctl","--user","reset-failed","container-devforge-pod-b"],
                   capture_output=True, timeout=10)
    print("  Starting Pod B with 14B Q8_0...", flush=True)
    subprocess.run(["systemctl","--user","start","container-devforge-pod-b"],
                   capture_output=True, timeout=60)
    for i in range(600):  # 최대 20분 대기
        try:
            resp = urllib.request.urlopen(
                urllib.request.Request("http://127.0.0.1:8082/health"), timeout=5)
            if resp.status == 200:
                print(f"  Ready in {i+1}s", flush=True)
                time.sleep(3)
                return True
        except Exception:
            pass
        if i % 60 == 0:
            print(f"  ... {i+1}s", flush=True)
        time.sleep(2)
    return False


def call_reviewer(system: str, user: str) -> dict:
    t0 = time.monotonic()
    try:
        meta = call_llm(
            [{"role":"system","content":system},
             {"role":"user","content":user}],
            model="extractor", max_tokens=1024, temperature=0.1,
            timeout=600, json_mode=True, return_meta=True,
        )
        raw = meta.get("content","")
        parsed = parse_llm_json(raw)
        elapsed = round(time.monotonic() - t0, 1)
        tokens = meta.get("usage",{}).get("completion_tokens",0)
        tps = round(tokens / elapsed, 2) if elapsed > 0 else 0
        return {"ok": parsed is not None, "parsed": parsed, "raw": raw[:300],
                "elapsed_s": elapsed, "tokens": tokens, "tps": tps}
    except Exception as e:
        elapsed = round(time.monotonic() - t0, 1)
        return {"ok": False, "parsed": None, "raw": str(e)[:300],
                "elapsed_s": elapsed, "tokens": 0, "tps": 0}


def run_test():
    print("="*70, flush=True)
    print("  [14B Q8_0 단일 Extract + MiniCheck Verify]", flush=True)
    print("  Pod B solo, 4 cores, 5 test cases", flush=True)
    print("="*70, flush=True)

    # Restart Pod B with 14B
    print("\n── Pod B 14B Q8_0 swap ──", flush=True)
    ok = restart_pod_b()
    if not ok:
        print("[FAIL] Pod B failed to start", flush=True)
        return
    print(flush=True)

    # Pre-warm: one empty call
    print("── Pre-warm ──", flush=True)
    r = call_reviewer(EXTRACT_PROMPT, "Brief test: return empty findings.")
    print(f"  Warm-up: {r['elapsed_s']}s, {r['tokens']}tok, {r['tps']}t/s\n", flush=True)

    results = []
    for i, gt in enumerate(GT):
        print(f"── [{i+1}/5] {gt['title']} ──", flush=True)
        ctx = build_context(gt)
        ctx_len = len(ctx)
        print(f"  Context: {ctx_len} chars", flush=True)

        # Step 1: 14B extract
        t1 = time.monotonic()
        resp = call_reviewer(EXTRACT_PROMPT, ctx)
        findings = []
        if resp["parsed"] and isinstance(resp["parsed"], dict):
            findings = resp["parsed"].get("findings", [])
        if not isinstance(findings, list):
            findings = []
        extract_s = resp["elapsed_s"]
        print(f"  14B extract: {extract_s}s, {len(findings)} findings, {resp['tps']}t/s", flush=True)

        # Step 2: MiniCheck verify
        t2 = time.monotonic()
        hallu_count = 0
        for f in findings:
            ev = f.get("evidence","")
            if ev and not mc_verify(ev, gt["text"]):
                hallu_count += 1
                f["_hallu"] = True
        mc_s = round(time.monotonic() - t2, 1)
        print(f"  MiniCheck: {mc_s}s, {hallu_count}/{len(findings)} hallucination", flush=True)

        # Score
        score, det = score_test(findings, gt["id"], hallu_count, gt["text"])
        total = round(time.monotonic() - t1, 1)
        print(f"  Score: {score}/100 ({det}) [total={total}s]", flush=True)
        if score < 50 and resp["raw"]:
            print(f"  Raw: {resp['raw'][:200]}", flush=True)
        print(flush=True)
        results.append({
            "id": gt["id"], "findings": len(findings), "hallu": hallu_count,
            "score": score, "det": det,
            "extract_s": extract_s, "mc_s": mc_s, "total_s": total,
            "tps": resp["tps"], "tokens": resp["tokens"],
        })

    # ── Report ──
    print("="*70, flush=True)
    print("  RESULTS", flush=True)
    print("="*70, flush=True)
    avg_s = sum(r["total_s"] for r in results)/len(results)
    avg_f = sum(r["findings"] for r in results)/len(results)
    avg_scores = sum(r["score"] for r in results)/len(results)
    print(f"\n  14B Q8_0 extract only:")
    print(f"    Average: {avg_s:.0f}s/case, {avg_f:.0f} findings")
    tps_list = [r["tps"] for r in results if r["tps"] > 0]
    if tps_list:
        print(f"    Speed: {sum(tps_list)/len(tps_list):.1f} t/s (avg)")
    print(f"    Accuracy: {avg_scores:.0f}/100")
    print(f"\n  14B + MiniCheck total:")
    print(f"    Average: {avg_s:.0f}s extract + ~5s MC = ~{avg_s+5:.0f}s/case")

    print(f"\n  ── Case Detail ──")
    for r in results:
        bar = "█" * int(r["score"]/10) + "░" * (10 - int(r["score"]/10))
        print(f"  {r['id']:15s} {r['score']:3d}/100 {bar}  {r['findings']}f/{r['hallu']}h  {r['total_s']:5.0f}s")

    # Restore 7B
    print(f"\n  Restoring 7B extractor...", flush=True)
    with open("/opt/ai_data/scripts/current-mode-pod-b.env","w") as f:
        f.write("MODE=day\nMODEL_NAME=extractor\nPORT=8082\nMODEL_FILE=Qwen2.5-Coder-7B-Instruct.Q8_0.gguf\nCTX_SIZE=8192\nTHREADS=2\nTHREADS_BATCH=2\nCACHE_RAM=512\n")
    subprocess.run(["systemctl","--user","restart","container-devforge-pod-b"],
                   capture_output=True, timeout=120)
    print("  Done", flush=True)


def score_test(findings, case_id, hallu_count, source_text):
    exp = EXPECT[case_id]
    n = len(findings)
    if exp["min"] == 0 and n > 0:
        return 10, f"false_pos({n})"
    if exp["min"] > 0 and n == 0:
        return 0, f"empty(exp={exp['min']})"
    cnt_ok = n >= exp["min"] if exp["min"] > 0 else True
    cats = set((f.get("category") or "").lower() for f in findings)
    cat_hits = sum(1 for c in exp["cats"] if c in cats)
    cat_ok = cat_hits >= len(exp["cats"]) if exp["cats"] else True
    h_pct = hallu_count / n * 100 if n > 0 else 0
    h_ok = h_pct <= exp["hallu_max"]
    pts = (20 if cnt_ok else 0) + (30 if cat_ok else 0) + (50 if h_ok else 0)
    det = f"{n}f cats={','.join(sorted(cats))} hallu={hallu_count}/{n}"
    return pts, det


if __name__ == "__main__":
    run_test()
