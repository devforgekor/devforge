#!/usr/bin/env python3
"""Resume A/B extract comparison from user_long_2000 onward.

Phase 1 of AB test: approach A + B on 4 remaining turns.
Phase snapshot saved to data/eval/ after each turn.
"""
import json, os, sys, time

SCRIPTS_DIR = "/opt/projects/server/scripts"
sys.path.insert(0, SCRIPTS_DIR)
os.chdir(SCRIPTS_DIR)

from lib.llm_client import call_llm
from lib.common import log
from pipelines.extract import _extract_for_turn

# ── Remaining turns ─────────────────────────────────────────
TURNS = [
    {"label": "user_long_2000", "user_turn": "이 서버에는 여러 파이프라인이 있습니다. extract는 사실 추출, enrich는 NLI 검증, entity_scan은 엔티티 스캔, day_verify는 최종 검증입니다. 각 파이프라인은 BATCH_LIMIT=10으로 설정되어 있고, watchdog이 상태를 관리합니다. 지금 inference에는 day-extractor(Qwen3-8B-Q8_0)가 떠있고, inference에는 reranker가 있습니다. 이 구조에서 throughput을 어떻게 개선할 수 있을까요? 특히 extract 단계가 가장 느린데, 병렬화나 청킹 전략 변경으로 개선 가능할까요?", "thinking": "User is asking about pipeline throughput optimization. Key bottleneck is extract phase. Options: batch_size tuning, parallel chunking, model swap.", "text": "분석 공유드립니다.\n\n**현재 구조 분석:**\n- Extract: turn당 200~400s, BATCH_LIMIT=10\n- Enrich: turn당 100~200s\n- Day_verify: turn당 20~30s\n\n**개선 방안:**\n1. _MAX_EXTRACT_CHARS 조정 (현재 5000→낮추면 chunking 빈도 증가)\n2. section 분리 (user/thinking/text 각각 추출)\n3. chunking sequential 전환 (parallel 경합 제거)\n4. 모델 변경 (Qwen3-8B → 더 작은 모델)\n\n추천: 2+3 조합이 가장 효과적일 것으로 예상됩니다."},
    {"label": "think_long_4k", "user_turn": "이슈가 뭐야?", "thinking": "The user is asking about the issue. Let me trace through the error. The error occurred in the enrichment phase. The NLI server at port 8085 returned a 404 error. This is because we removed the DeBERTa-v3 NLI server from inference but forgot to update the enrich pipeline. The enrich.py was still calling _call_nli_server(port=8085) which doesn't exist anymore. We fixed this by switching to LLM self-verify NLI instead. The fix was to use call_llm with day_enrich model and max_tokens=16, temperature=0.0, with a 60s timeout. This actually works better because it catches logical contradictions that the reranker would miss. The reranker only measures topical relevance, not logical entailment.", "text": "NLI 서버 8085 미배포 문제였습니다. enrich.py가 존재하지 않는 DeBERTa-v3 서버를 호출하고 있었고, LLM self-verify로 교체했습니다."},
    {"label": "text_long_4k", "user_turn": "상세한 설명 부탁해", "thinking": "User wants detailed explanation of the architecture. Need to cover all components.", "text": "DevForge 아키텍처를 상세히 설명드립니다.\n\n## 전체 구조\n서버는 inference와 inference로 구성됩니다. inference에는 reranker(Qwen3-Reranker)가 8080번 포트에서 동작합니다. inference는 상황에 따라 모델이 전환됩니다. day-extractor(Qwen3-8B-Q8_0), night-validator(Qwen3-8B-Q8_0), assistant(Qwen2.5-7B-Q4_K_M) 등이 필요에 따라 로드됩니다.\n\n## 파이프라인\n1. embed_batch: 턴 텍스트를 임베딩 벡터로 변환 (8081)\n2. entity_scan: 정규식 기반 엔티티 스캔 (LLM 불필요)\n3. extract: 사실 추출 (가장 느림, turn당 200~400s)\n4. enrich: NLI 검증 + TLDR 요약\n5. day_verify: 최종 응답 검증\n\n## 데이터 흐름\nPostgreSQL 16에 모든 데이터 저장. review_facts 테이블이 중앙 저장소. 각 파이프라인은 NOT EXISTS anti-join으로 미처리 턴만 선택.\n\n## 성능 특성\n- 평균 throughput: turn당 ~388s\n- chunking이 필요한 케이스: text 5000자 초과 (약 10%)\n- parallel=2 사용 시 slot 경합으로 각각 1.3~1.5배 느려짐\n\n## 최근 변경사항\n- enrich NLI 8085→LLM self-verify\n- entity_scan backlog fix (conv_ents 제외)\n- BATCH_LIMIT 10 통일\n- GEN_TIME_BUF 300→750으로 extract timeout 개선\n- section 분리 추출 (user/thinking/text 각각 호출)"},
    {"label": "mixed_long_12k", "user_turn": "첫 번째로 inference와 inference의 협력 구조를 개선해야 합니다. 현재는 inference가 reranker로만 사용되는데, MoE 구조를 도입하면 좋겠습니다. 두 번째로 PostgreSQL 쿼리 최적화가 필요합니다. review_facts 테이블에 인덱스가 부족해서 NOT EXISTS 쿼리가 500ms 이상 걸립니다. 세 번째로 watchog의 heartbeat 메커니즘을 개선해야 합니다. 네 번째로 Caddy 리버스 프록시 설정을 검토해야 합니다. 다섯 번째로 백업 전략을 재수립해야 합니다. 여섯 번째로 메트릭 수집 및 대시보드를 구축해야 합니다. 일곱 번째로 CI/CD 파이프라인을 구축해야 합니다. 여덟 번째로 모델 업데이트 자동화를 검토해야 합니다. 아홉 번째로 시큐리티 감사 로그를 활성화해야 합니다. 열 번째로 리소스 사용량 알림을 설정해야 합니다.", "thinking": "Ten key improvement areas identified. Need to prioritize based on impact. inference/B MoE restructuring is highest impact but most complex. Query optimization is quick win. Let me assess each area.", "text": "10개 개선 영역을 우선순위별로 정리했습니다.\n\n**Priority 1 (즉시):**\n1. PostgreSQL 인덱스 최적화 — NOT EXISTS 쿼리 500ms→5ms, 즉시 적용 가능\n2. Watchdog heartbeat 개선 — staleness timeout 조정\n3. 리소스 알림 설정 — disk 93% 알림\n\n**Priority 2 (1~2주):**\n4. 메트릭 대시보드 — Netdata + Grafana\n5. 백업 전략 재수립 — WAL + daily snapshot\n6. CI/CD — GitHub Actions + systemd 배포\n\n**Priority 3 (장기):**\n7. inference/B MoE — 구조 개선, 모델 병렬화\n8. Caddy 리버스 프록시 설정 — rate limit + WAF\n9. 시큐리티 감사 로그 — auditd + 원격 로깅\n10. 모델 업데이트 자동화 — 주기적 GGUF 갱신\n\n각 항목별 상세 계획은 별도 문서로 준비하겠습니다."},
]

# ── Approach A prompt ───────────────────────────────────────
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

def approach_a(turn):
    parts = [
        "=== user_turn ===", turn.get("user_turn",""), "",
        "=== thinking ===", turn.get("thinking",""), "",
        "=== text ===", turn.get("text",""),
    ]
    total_chars = len(turn.get("user_turn","")) + len(turn.get("thinking","")) + len(turn.get("text",""))
    timeout = min(60 + int(total_chars * 0.2) + 750, 1800)
    t0 = time.monotonic()
    meta = call_llm(
        [{"role": "system", "content": PROMPT_A},
         {"role": "user", "content": "\n".join(parts)}],
        model="day_extract", max_tokens=512, temperature=0.1,
        timeout=timeout, json_mode=True, return_meta=True,
    )
    elapsed = time.monotonic() - t0
    raw = meta.get("content", "")
    try:
        parsed = json.loads(raw)
    except:
        return {"error": f"JSON parse failed: {raw[:100]}", "elapsed_s": round(elapsed, 1)}
    ex = parsed.get("extractions", [])
    by_type = {}
    for e in ex:
        by_type[e.get("fact_type","unknown")] = by_type.get(e.get("fact_type","unknown"), 0) + 1
    return {"extractions": ex, "by_type": by_type, "total": len(ex), "elapsed_s": round(elapsed, 1)}

def approach_b(turn):
    turn_dict = {"id": "00000000-0000-0000-0000-000000000000",
                 "user_turn": turn.get("user_turn",""),
                 "thinking": turn.get("thinking",""),
                 "text": turn.get("text","")}
    t0 = time.monotonic()
    _, result, error = _extract_for_turn(turn_dict)
    elapsed = time.monotonic() - t0
    if error:
        return {"error": error, "elapsed_s": round(elapsed, 1), "result": result}
    if not result:
        return {"extractions": [], "by_type": {}, "total": 0, "elapsed_s": round(elapsed, 1)}
    ex = result.get("extractions", [])
    by_type = {}
    for e in ex:
        by_type[e.get("fact_type","unknown")] = by_type.get(e.get("fact_type","unknown"), 0) + 1
    return {"extractions": ex, "by_type": by_type, "total": len(ex), "elapsed_s": round(elapsed, 1)}

def save_snapshot(label, results, phase="A"):
    path = f"data/eval/extract_ab_{label}_{phase}_snapshot.json"
    with open(path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    log(f"  snapshot saved: {path}")

# ── Main ─────────────────────────────────────────────────────
log("=" * 70)
log("EXTRACT A/B COMPARISON — RESUME")
log("Remaining: user_long_2000, think_long_4k, text_long_4k, mixed_long_12k")
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

    for app_name, app_fn in [("A", lambda t=turn: approach_a(t)),
                              ("B", lambda t=turn: approach_b(t))]:
        log(f"  [{app_name}] starting...")
        try:
            res = app_fn(turn)
            if "error" in res:
                log(f"  [{app_name}] ERROR: {res['error']}")
                results[label][app_name] = {"error": res["error"],
                                             "elapsed_s": res.get("elapsed_s", 0)}
            else:
                log(f"  [{app_name}] user={res['by_type'].get('user',0)} "
                    f"think={res['by_type'].get('thinking',0)} "
                    f"text={res['by_type'].get('text',0)} "
                    f"total={res['total']} time={res['elapsed_s']}s")
                results[label][app_name] = {
                    "by_type": res["by_type"], "total": res["total"],
                    "elapsed_s": res["elapsed_s"]}
            # Save snapshot after every approach completion
            save_snapshot(label, results[label], app_name)
        except Exception as e:
            log(f"  [{app_name}] EXCEPTION: {type(e).__name__}: {e}")
            results[label][app_name] = {"error": f"{type(e).__name__}: {str(e)[:200]}",
                                         "elapsed_s": 0}
            save_snapshot(label, results[label], f"{app_name}_ERROR")

# ── Summary ──────────────────────────────────────────────────
log("\n" + "=" * 70)
log("PHASE 1 COMPLETE — Final Summary")
log("=" * 70)
log(f"{'Turn':<20} {'A':<30} {'B':<30}")
log("-" * 80)
for turn in TURNS:
    label = turn["label"]
    a = results[label].get("A", {})
    b = results[label].get("B", {})
    a_str = f"u={a.get('by_type',{}).get('user',0)} t={a.get('by_type',{}).get('thinking',0)} x={a.get('by_type',{}).get('text',0)} tot={a.get('total','?')} {a.get('elapsed_s',0):.0f}s" if "by_type" in a else f"ERR: {a.get('error','?')[:30]}"
    b_str = f"u={b.get('by_type',{}).get('user',0)} t={b.get('by_type',{}).get('thinking',0)} x={b.get('by_type',{}).get('text',0)} tot={b.get('total','?')} {b.get('elapsed_s',0):.0f}s" if "by_type" in b else f"ERR: {b.get('error','?')[:30]}"
    log(f"{label:<20} {a_str:<30} {b_str:<30}")

# Save final report
report_path = f"data/eval/extract_ab_phase1_{time.strftime('%Y%m%d_%H%M%S')}.json"
with open(report_path, "w") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
log(f"\nPhase 1 report: {report_path}")
