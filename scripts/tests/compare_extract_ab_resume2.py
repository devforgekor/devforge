#!/usr/bin/env python3
"""Resume2 — Auto-recover A/B test from think_long_4k onward.

Handles 8082 crashes by auto-reloading model and retrying.
"""
import json, os, sys, time, traceback

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.llm_client import call_llm
from lib.common import log
from pipelines.extract import _extract_for_turn

def reload_8082():
    """Load day-extractor on 8082."""
    from lib.pod_manager import ensure_model
    log("  [recovery] Reloading 8082...")
    ensure_model('day-extractor', skip_if_healthy=False)
    log("  [recovery] 8082 ready")

PROMPT_A = """\
You are a fact extractor for a developer-assistant conversation turn.
Each turn has three parts: user_turn (the user's message), thinking (the
model's internal reasoning, may be empty), and text (the model's response).

Extract key factual statements that are EXPLICITLY present in the text.
Do NOT infer, summarize, or add information not present in the source.

Output STRICT JSON:
{
  "extractions": [
    {
      "fact_type": "user|thinking|text",
      "evidence": "Exact quote or close paraphrase from the source",
      "category": "requirement|decision|explanation|code|reasoning|other"
    }
  ]
}

Rules:
- fact_type must match which source field the evidence came from
- evidence must be directly traceable to the source text
- Skip thinking if it is empty or contains only formatting
- Extract at most 5 facts per fact_type
- If nothing extractable, return {"extractions": []}"""

def call_with_retry(fn, max_retries=3, retry_delay=30):
    """Call fn, auto-retry on 8082 errors with model reload."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            err_str = str(e)
            is_8082_error = any(x in err_str for x in ["8082", "Broken pipe", "Connection reset",
                                                        "Connection refused", "Remote end closed",
                                                        "timeout", "timed out"])
            if is_8082_error and attempt < max_retries:
                log(f"  [retry {attempt}/{max_retries}] 8082 error: {type(e).__name__}")
                if attempt == 1 or attempt == 2:
                    reload_8082()
                else:
                    log(f"  [retry] waiting {retry_delay}s...")
                    time.sleep(retry_delay)
            else:
                raise

def run_a(turn):
    total_chars = len(turn.get("user_turn","")) + len(turn.get("thinking","")) + len(turn.get("text",""))
    timeout = min(60 + int(total_chars * 0.2) + 750, 1800)
    parts = ["=== user_turn ===", turn.get("user_turn",""), "",
             "=== thinking ===", turn.get("thinking",""), "",
             "=== text ===", turn.get("text","")]
    def _call():
        t0 = time.monotonic()
        meta = call_llm([{"role": "system", "content": PROMPT_A},
                         {"role": "user", "content": "\n".join(parts)}],
                        model="day_extract", max_tokens=512, temperature=0.1,
                        timeout=timeout, json_mode=True, return_meta=True)
        elapsed = time.monotonic() - t0
        raw = meta.get("content","")
        parsed = json.loads(raw)
        ex = parsed.get("extractions", [])
        by_type = {}
        for e in ex:
            by_type[e.get("fact_type","unknown")] = by_type.get(e.get("fact_type","unknown"), 0) + 1
        return {"extractions": ex, "by_type": by_type, "total": len(ex), "elapsed_s": round(elapsed, 1)}
    return call_with_retry(_call)

def run_b(turn):
    turn_dict = {"id": "00000000-0000-0000-0000-000000000000",
                 "user_turn": turn.get("user_turn",""),
                 "thinking": turn.get("thinking",""),
                 "text": turn.get("text","")}
    def _call():
        t0 = time.monotonic()
        _, result, error = _extract_for_turn(turn_dict)
        elapsed = time.monotonic() - t0
        if error:
            return {"error": error, "elapsed_s": round(elapsed, 1)}
        if not result:
            return {"extractions": [], "by_type": {}, "total": 0, "elapsed_s": round(elapsed, 1)}
        ex = result.get("extractions", [])
        by_type = {}
        for e in ex:
            by_type[e.get("fact_type","unknown")] = by_type.get(e.get("fact_type","unknown"), 0) + 1
        return {"extractions": ex, "by_type": by_type, "total": len(ex), "elapsed_s": round(elapsed, 1)}
    return call_with_retry(_call)

TURNS = [
    {"label": "user_long_2000_B_only",
     "description": "Retry B approach only (A succeeded before 8082 crash)",
     "user_turn": "이 서버에는 여러 파이프라인이 있습니다. extract는 사실 추출, enrich는 NLI 검증, entity_scan은 엔티티 스캔, day_verify는 최종 검증입니다. 각 파이프라인은 BATCH_LIMIT=10으로 설정되어 있고, watchdog이 상태를 관리합니다. 지금 Pod B에는 day-extractor(Qwen3-8B-Q8_0)가 떠있고, Pod A에는 reranker가 있습니다. 이 구조에서 throughput을 어떻게 개선할 수 있을까요? 특히 extract 단계가 가장 느린데, 병렬화나 청킹 전략 변경으로 개선 가능할까요?",
     "thinking": "User is asking about pipeline throughput optimization. Key bottleneck is extract phase.",
     "text": "분석 공유드립니다.\n\n**현재 구조 분석:**\n- Extract: turn당 200~400s, BATCH_LIMIT=10\n- Enrich: turn당 100~200s\n- Day_verify: turn당 20~30s\n\n**개선 방안:**\n1. _MAX_EXTRACT_CHARS 조정\n2. section 분리 (user/thinking/text 각각 추출)\n3. chunking sequential 전환\n4. 모델 변경\n\n추천: 2+3 조합이 가장 효과적일 것으로 예상됩니다.",
     "skip_a": True},
    {"label": "think_long_4k",
     "user_turn": "이슈가 뭐야?",
     "thinking": "The user is asking about the issue. Let me trace through the error. The error occurred in the enrichment phase. The NLI server at port 8085 returned a 404 error. This is because we removed the DeBERTa-v3 NLI server from Pod A but forgot to update the enrich pipeline. The enrich.py was still calling _call_nli_server(port=8085) which doesn't exist anymore. We fixed this by switching to LLM self-verify NLI instead. The fix was to use call_llm with day_enrich model and max_tokens=16, temperature=0.0, with a 60s timeout. This actually works better because it catches logical contradictions that the reranker would miss. The reranker only measures topical relevance, not logical entailment.",
     "text": "NLI 서버 8085 미배포 문제였습니다. enrich.py가 존재하지 않는 DeBERTa-v3 서버를 호출하고 있었고, LLM self-verify로 교체했습니다."},
    {"label": "text_long_4k",
     "user_turn": "상세한 설명 부탁해",
     "thinking": "User wants detailed explanation of the architecture.",
     "text": "DevForge 아키텍처를 상세히 설명드립니다.\n\n## 전체 구조\n서버는 Pod A와 Pod B로 구성됩니다. Pod A에는 reranker(Qwen3-Reranker)가 8080번 포트에서 동작합니다. Pod B는 상황에 따라 모델이 전환됩니다. day-extractor(Qwen3-8B-Q8_0), night-validator(Qwen3-8B-Q8_0), assistant(Qwen2.5-7B-Q4_K_M) 등이 필요에 따라 로드됩니다.\n\n## 파이프라인\n1. embed_batch: 턴 텍스트를 임베딩 벡터로 변환 (8081)\n2. entity_scan: 정규식 기반 엔티티 스캔 (LLM 불필요)\n3. extract: 사실 추출 (가장 느림, turn당 200~400s)\n4. enrich: NLI 검증 + TLDR 요약\n5. day_verify: 최종 응답 검증\n\n## 데이터 흐름\nPostgreSQL 16에 모든 데이터 저장. review_facts 테이블이 중앙 저장소.\n\n## 성능 특성\n- 평균 throughput: turn당 ~388s\n- chunking이 필요한 케이스: text 5000자 초과 (약 10%)\n- parallel=2 사용 시 slot 경합으로 각각 1.3~1.5배 느려짐\n\n## 최근 변경사항\n- enrich NLI 8085→LLM self-verify\n- entity_scan backlog fix (conv_ents 제외)\n- BATCH_LIMIT 10 통일\n- section 분리 추출 (user/thinking/text 각각 호출)"},
    {"label": "mixed_long_12k",
     "user_turn": "첫 번째로 Pod A와 Pod B의 협력 구조를 개선해야 합니다. 현재는 Pod A가 reranker로만 사용되는데, MoE 구조를 도입하면 좋겠습니다. 두 번째로 PostgreSQL 쿼리 최적화가 필요합니다. 세 번째로 watchog heartbeat를 개선해야 합니다. 네 번째로 Caddy 리버스 프록시 설정을 검토해야 합니다. 다섯 번째로 백업 전략을 재수립해야 합니다.",
     "thinking": "Five key improvement areas. Need to prioritize.",
     "text": "10개 개선 영역을 우선순위별로 정리했습니다.\n\n**Priority 1 (즉시):**\n1. PostgreSQL 인덱스 최적화\n2. Watchdog heartbeat 개선\n3. 리소스 알림 설정\n\n**Priority 2 (1~2주):**\n4. 메트릭 대시보드\n5. 백업 전략 재수립\n6. CI/CD\n\n**Priority 3 (장기):**\n7. Pod A/B MoE\n8. Caddy 리버스 프록시 설정\n9. 시큐리티 감사 로그\n10. 모델 업데이트 자동화"},
]

log("=" * 70)
log("EXTRACT A/B COMPARISON — RESUME2 (auto-recovery)")
log("=" * 70)

results = {}
for turn in TURNS:
    label = turn["label"]
    log(f"\n--- {label} ---")
    log(f"  user={len(turn.get('user_turn',''))}ch "
        f"think={len(turn.get('thinking',''))}ch "
        f"text={len(turn.get('text',''))}ch")

    results[label] = {"turn_sizes": {"user": len(turn.get("user_turn","")),
                                      "thinking": len(turn.get("thinking","")),
                                      "text": len(turn.get("text",""))}}

    if not turn.get("skip_a"):
        log(f"  [A] starting (auto-retry)...")
        try:
            res = run_a(turn)
            log(f"  [A] user={res['by_type'].get('user',0)} think={res['by_type'].get('thinking',0)} text={res['by_type'].get('text',0)} total={res['total']} time={res['elapsed_s']}s")
            results[label]["A"] = {"by_type": res["by_type"], "total": res["total"],
                                    "elapsed_s": res["elapsed_s"]}
        except Exception as e:
            log(f"  [A] ALL RETRIES EXHAUSTED: {type(e).__name__}: {str(e)[:100]}")
            results[label]["A"] = {"error": f"{type(e).__name__}: {str(e)[:100]}", "elapsed_s": 0}
        # Snapshot after A
        with open(f"data/eval/extract_ab_{label}_A_snapshot.json", "w") as f:
            json.dump(results[label], f, indent=2, ensure_ascii=False)

    log(f"  [B] starting (auto-retry)...")
    try:
        res = run_b(turn)
        err = res.get("error")
        if err:
            log(f"  [B] result: {err}")
            results[label]["B"] = {"error": err, "elapsed_s": res.get("elapsed_s",0)}
        else:
            log(f"  [B] user={res['by_type'].get('user',0)} think={res['by_type'].get('thinking',0)} text={res['by_type'].get('text',0)} total={res['total']} time={res['elapsed_s']}s")
            results[label]["B"] = {"by_type": res["by_type"], "total": res["total"],
                                    "elapsed_s": res["elapsed_s"]}
    except Exception as e:
        log(f"  [B] ALL RETRIES EXHAUSTED: {type(e).__name__}: {str(e)[:100]}")
        results[label]["B"] = {"error": f"{type(e).__name__}: {str(e)[:100]}", "elapsed_s": 0}
    # Snapshot after B
    with open(f"data/eval/extract_ab_{label}_B_snapshot.json", "w") as f:
        json.dump(results[label], f, indent=2, ensure_ascii=False)

# ── Summary ──────────────────────────────────────────────────
log("\n" + "=" * 70)
log("PHASE 2 (RESUME) COMPLETE")
log("=" * 70)
for turn in TURNS:
    label = turn["label"]
    log(f"\n{label}:")
    for app in ["A", "B"]:
        d = results[label].get(app, {})
        if "by_type" in d:
            log(f"  {app}: user={d['by_type'].get('user',0)} think={d['by_type'].get('thinking',0)} text={d['by_type'].get('text',0)} total={d['total']} {d['elapsed_s']:.0f}s")
        else:
            log(f"  {app}: ERROR={d.get('error','?')[:60]}")

report = f"data/eval/extract_ab_phase2_{time.strftime('%Y%m%d_%H%M%S')}.json"
with open(report, "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
log(f"\nPhase 2 report: {report}")
