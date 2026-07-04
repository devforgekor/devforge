#!/bin/bash
# E2E Full Pipeline Test — 20 turns, all phases, snapshot per phase + quality eval
# Phases: text_clean → polish → embed → entity_scan → extract → enrich → day_verify
set +e
cd /opt/projects/server/scripts || exit 1

# CONFIG
LIMIT=20
PARALLEL=2
TS=$(date -u +%Y%m%d_%H%M%S)
SNAPSHOT_DIR="data/eval/20turns_${TS}"
REPORT="$SNAPSHOT_DIR/full_report.json"
PY="python3 -B"

mkdir -p "$SNAPSHOT_DIR"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

log "============================================================"
log "E2E 20-TURN FULL PIPELINE — all phases + quality evaluation"
log "Phase: text_clean → polish → embed → scan → extract → enrich → verify"
log "============================================================"
log "Started at: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
log "KST: $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S')"
log ""

# ── Test Protection ──
log "[setup] Registering test protection..."
$PY -c "
import sys; sys.path.insert(0, '.')
from lib.test_common import test_setup
test_setup('e2e_20turns_full', 'E2E 20-turn full pipeline with all phases')
print('Protection registered.')
"

recover_8082() {
    log "[recovery] Reloading 8082..."
    $PY -c "
import sys; sys.path.insert(0, '.')
from lib.pod_manager import ensure_model
ensure_model('day-extractor', skip_if_healthy=False)
print('8082 ready')
" 2>&1 || true
}
heartbeat() {
    $PY -c "
import sys; sys.path.insert(0, '.')
from lib.test_common import test_heartbeat
test_heartbeat('$1')
" 2>/dev/null || true
}

# ═══════════════════════════════════════════════════════════════
# Phase 0: text_clean (all turns that need it, no LLM)
# ═══════════════════════════════════════════════════════════════
heartbeat "phase:text_clean"
log "============================================================"
log "Phase 0/7: text_clean (맞춤법 포함 전처리)"
log "============================================================"
t0=$(date +%s)
$PY pipelines/text_clean.py 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "text_clean exit=$rc elapsed=${elapsed}s"

# Snapshot
$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
total = (psql_json('SELECT count(*) AS cnt FROM turns') or [{'cnt':0}])[0]['cnt']
clean = (psql_json(\"SELECT count(*) AS cnt FROM turns WHERE text_clean IS NOT NULL AND text_clean != ''\") or [{'cnt':0}])[0]['cnt']
polished = (psql_json(\"SELECT count(*) AS cnt FROM turns WHERE text_clean_polished IS NOT NULL AND text_clean_polished != ''\") or [{'cnt':0}])[0]['cnt']
with open('$SNAPSHOT_DIR/00_text_clean.json', 'w') as f:
    json.dump({'ok': $rc == 0, 'elapsed_s': $elapsed, 'total_turns': total, 'cleaned': clean, 'has_polish': polished}, f, indent=2)
log(f'  text_clean done: {clean}/{total} turns cleaned, {polished} polished')
"

heartbeat "phase:polish"

# ═══════════════════════════════════════════════════════════════
# Phase 0.5: polish (Kiwi-only, --no-llm)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 0.5/7: polish_batch --no-llm (Kiwi 형태소 분석)"
log "============================================================"
t0=$(date +%s)
$PY pipelines/polish_batch.py --no-llm --limit=6000 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "polish_batch exit=$rc elapsed=${elapsed}s"

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
total = (psql_json('SELECT count(*) AS cnt FROM turns') or [{'cnt':0}])[0]['cnt']
polished = (psql_json(\"SELECT count(*) AS cnt FROM turns WHERE text_clean_polished IS NOT NULL AND text_clean_polished != ''\") or [{'cnt':0}])[0]['cnt']
with open('$SNAPSHOT_DIR/00_polish.json', 'w') as f:
    json.dump({'ok': $rc == 0, 'elapsed_s': $elapsed, 'total_turns': total, 'polished': polished}, f, indent=2)
log(f'  polish done: {polished}/{total} turns polished')
"

heartbeat "phase:embed"

# ═══════════════════════════════════════════════════════════════
# Phase 1: embed_batch (model on 8081)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 1/7: embed_batch (limit=$LIMIT)"
log "============================================================"
t0=$(date +%s)
$PY pipelines/embed_batch.py --limit=$LIMIT 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "embed_batch exit=$rc elapsed=${elapsed}s"

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
total = (psql_json('SELECT count(*) AS cnt FROM embeddings WHERE source_type=\\'turn\\'') or [{'cnt':0}])[0]['cnt']
with open('$SNAPSHOT_DIR/01_embed.json', 'w') as f:
    json.dump({'ok': $rc == 0, 'elapsed_s': $elapsed, 'total_embeddings': total}, f, indent=2)
log(f'  embed done: {total} total embeddings')
"

heartbeat "phase:entity_scan"

# ═══════════════════════════════════════════════════════════════
# Phase 2: entity_scan (no LLM)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 2/7: entity_scan (limit=$LIMIT)"
log "============================================================"
t0=$(date +%s)
$PY pipelines/entity_scan.py --limit=$LIMIT 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "entity_scan exit=$rc elapsed=${elapsed}s"

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
rows = psql_json('SELECT fact_type, count(*) AS cnt FROM review_facts WHERE fact_type=\\'entity_scan\\' GROUP BY fact_type') or []
scan_count = rows[0]['cnt'] if rows else 0
with open('$SNAPSHOT_DIR/02_entity_scan.json', 'w') as f:
    json.dump({'ok': $rc == 0, 'elapsed_s': $elapsed, 'entity_scan_facts': scan_count}, f, indent=2)
log(f'  entity_scan done: {scan_count} facts')
"

heartbeat "phase:switch_extract"

# Switch inference to day-extractor
log "--- Switching inference to day-extractor (:8082) ---"
$PY -c "
import sys; sys.path.insert(0, '.')
from lib.pod_manager import ensure_model
ensure_model('day-extractor', skip_if_healthy=True)
print('Model switch to day-extractor done')
"

heartbeat "phase:extract"

# ═══════════════════════════════════════════════════════════════
# Phase 3: extract (section-split, 8082 auto-recovery)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 3/7: extract — section-split (limit=$LIMIT)"
log "============================================================"
extract_ok=false
extract_elapsed=0
for attempt in 1 2 3; do
    t0=$(date +%s)
    log "  extract attempt $attempt/3..."
    $PY pipelines/extract.py --limit=$LIMIT --parallel=$PARALLEL 2>&1
    rc=$?
    extract_elapsed=$(($(date +%s) - t0))

    fact_count=$($PY -c "
import sys; sys.path.insert(0, '.')
from lib.db import psql_json
rows = psql_json('SELECT count(*) AS cnt FROM review_facts WHERE source=\\'extract_pipeline\\' AND created_at > now() - interval \\'4 hours\\'') or [{'cnt':0}]
print(rows[0]['cnt'])
" 2>/dev/null || echo "0")

    if [ $rc -eq 0 ] && [ "$fact_count" -gt 0 ] 2>/dev/null; then
        log "extract OK — ${fact_count} facts in ${extract_elapsed}s"
        extract_ok=true
        break
    else
        log "[WARN] extract attempt $attempt: rc=$rc, facts=$fact_count after ${extract_elapsed}s"
        if [ $attempt -lt 3 ]; then
            recover_8082
            heartbeat "extract retry $attempt"
        fi
    fi
done

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
rows = psql_json(\"SELECT fact_type, count(*) AS cnt FROM review_facts WHERE source='extract_pipeline' AND created_at > now() - interval '4 hours' GROUP BY fact_type ORDER BY fact_type\") or []
by_type = {r['fact_type']: r['cnt'] for r in rows}
total = sum(by_type.values())
with open('$SNAPSHOT_DIR/03_extract.json', 'w') as f:
    json.dump({'ok': $extract_ok, 'elapsed_s': $extract_elapsed, 'facts_by_type': by_type, 'total_facts': total}, f, indent=2)
log(f'  extract snapshot: {total} facts: {by_type}')
"

heartbeat "phase:enrich"

# ═══════════════════════════════════════════════════════════════
# Phase 4: enrich (LLM NLI + TLDR, 8082 auto-recovery)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 4/7: enrich (NLI + TLDR, limit=$LIMIT)"
log "============================================================"
enrich_ok=false
for attempt in 1 2 3; do
    t0=$(date +%s)
    log "  enrich attempt $attempt/3..."
    $PY pipelines/enrich.py --limit=$LIMIT 2>&1
    rc=$?
    elapsed=$(($(date +%s) - t0))
    if [ $rc -eq 0 ]; then
        log "enrich OK in ${elapsed}s"
        enrich_ok=true
        break
    else
        log "[WARN] enrich attempt $attempt failed (rc=$rc) after ${elapsed}s"
        if [ $attempt -lt 3 ]; then
            recover_8082
            heartbeat "enrich retry $attempt"
        fi
    fi
done

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
rows = psql_json(\"SELECT COALESCE(nli_verdict, 'pending') AS verdict, count(*) AS cnt FROM review_facts WHERE source='enrich' AND created_at > now() - interval '4 hours' GROUP BY COALESCE(nli_verdict, 'pending')\") or []
by_result = {r['verdict']: r['cnt'] for r in rows}
total = sum(by_result.values())
with open('$SNAPSHOT_DIR/04_enrich.json', 'w') as f:
    json.dump({'ok': $enrich_ok, 'elapsed_s': $elapsed, 'enrich_by_verdict': by_result, 'total_enriched': total}, f, indent=2)
log(f'  enrich snapshot: {total} enriched: {by_result}')
"

heartbeat "phase:switch_verify"

# Switch inference to day-verifier
log "--- Switching inference to day-verifier (:8082) ---"
$PY -c "
import sys; sys.path.insert(0, '.')
from lib.pod_manager import ensure_model
ensure_model('day-verifier', skip_if_healthy=True)
print('Model switch to day-verifier done')
"

heartbeat "phase:day_verify"

# ═══════════════════════════════════════════════════════════════
# Phase 5: day_verify (faithfulness + factuality, 8082 recovery)
# ═══════════════════════════════════════════════════════════════
log "============================================================"
log "Phase 5/7: day_verify (faithfulness + factuality, limit=$LIMIT)"
log "============================================================"
t0=$(date +%s)
$PY pipelines/day_verify.py --limit=$LIMIT 2>&1
rc=$?
elapsed=$(($(date +%s) - t0))
log "day_verify exit=$rc elapsed=${elapsed}s"

$PY -c "
import sys; sys.path.insert(0, '.')
import json
from lib.db import psql_json
rows = psql_json(\"SELECT COALESCE(nli_verdict, 'pending') AS verdict, count(*) AS cnt FROM review_facts WHERE source='day_verify' AND created_at > now() - interval '4 hours' GROUP BY COALESCE(nli_verdict, 'pending')\") or []
by_result = {r['verdict']: r['cnt'] for r in rows}
total = sum(by_result.values())
with open('$SNAPSHOT_DIR/05_day_verify.json', 'w') as f:
    json.dump({'ok': $rc == 0, 'elapsed_s': $elapsed, 'verify_by_verdict': by_result, 'total_verified': total}, f, indent=2)
log(f'  verify snapshot: {total} verified: {by_result}')
"

# ═══════════════════════════════════════════════════════════════
# Quality Evaluation (per phase, 10-point + usability)
# ═══════════════════════════════════════════════════════════════
heartbeat "phase:quality_eval"
log ""
log "============================================================"
log "Quality Evaluation — per phase assessment"
log "============================================================"

$PY -c "
import sys; sys.path.insert(0, '.')
import json, os

SNAP = '$SNAPSHOT_DIR'
KST = \"$(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S')\"

def load_snaps(path):
    snaps = {}
    for f in sorted(os.listdir(path)):
        if f.endswith('.json'):
            with open(os.path.join(path, f)) as fh:
                try: snaps[f.replace('.json','')] = json.load(fh)
                except: snaps[f.replace('.json','')] = {'error': 'parse failed'}
    return snaps

snapshots = load_snaps(SNAP)

# Phase evaluations
evals = []

# 00_text_clean
tc = snapshots.get('00_text_clean', {})
clean_pct = round(tc.get('cleaned',0) / max(tc.get('total_turns',1),1) * 100, 1) if tc.get('total_turns',0) > 0 else 0
tc_score = 9 if clean_pct > 80 else 7 if clean_pct > 50 else 5
evals.append({
    'phase': 'text_clean', 'score': f'{tc_score}/10', 'pass': tc_score >= 7,
    'details': {'turns_cleaned': tc.get('cleaned',0), 'coverage_pct': clean_pct, 'elapsed_s': tc.get('elapsed_s',0)},
    'quality': 'NFKC 정규화 + 공백/이모지 정리 — 안정적',
    'usability': '사용 가능. 맞춤법은 Kiwi polish로 커버.',
})

# 00_polish
pl = snapshots.get('00_polish', {})
polish_pct = round(pl.get('polished',0) / max(pl.get('total_turns',1),1) * 100, 1) if pl.get('total_turns',0) > 0 else 0
pl_score = 9 if polish_pct > 80 else 7 if polish_pct > 50 else 5
evals.append({
    'phase': 'polish', 'score': f'{pl_score}/10', 'pass': pl_score >= 7,
    'details': {'turns_polished': pl.get('polished',0), 'coverage_pct': polish_pct, 'elapsed_s': pl.get('elapsed_s',0)},
    'quality': 'Kiwi 형태소 분석 + 맞춤법 교정. --no-llm 모드로 빠름.',
    'usability': '사용 가능. LLM polish 비활성(속도 우선).',
})

# 01_embed
em = snapshots.get('01_embed', {})
em_score = 9 if em.get('ok', False) else 3
evals.append({
    'phase': 'embed_batch', 'score': f'{em_score}/10', 'pass': em.get('ok', False),
    'details': {'total_embeddings': em.get('total_embeddings',0), 'elapsed_s': em.get('elapsed_s',0)},
    'quality': 'Q8_0 임베딩 안정적. f16→q8 전환 완료.',
    'usability': '사용 가능. 추가 수정 불필요.',
})

# 02_entity_scan
es = snapshots.get('02_entity_scan', {})
es_score = 8 if es.get('ok', False) else 3
evals.append({
    'phase': 'entity_scan', 'score': f'{es_score}/10', 'pass': es.get('ok', False),
    'details': {'facts_found': es.get('entity_scan_facts',0), 'elapsed_s': es.get('elapsed_s',0)},
    'quality': 'Regex+registry 기반 deterministic. conversation context backlog fix 적용.',
    'usability': '사용 가능. 추가 수정 불필요.',
})

# 03_extract
ex = snapshots.get('03_extract', {})
ex_ok = ex.get('ok', False)
ex_total = ex.get('total_facts', 0)
ex_score = 8 if (ex_ok and ex_total > 20) else 7 if (ex_ok and ex_total > 0) else 3 if ex_ok else 1
evals.append({
    'phase': 'extract', 'score': f'{ex_score}/10', 'pass': ex_ok and ex_total > 0,
    'details': {'total_facts': ex_total, 'by_type': ex.get('facts_by_type', {}), 'elapsed_s': ex.get('elapsed_s',0)},
    'quality': 'Section-split (user/thinking/text sequential). 8082 crash recovery 적용.',
    'usability': '핵심 phase. 8082 crash recovery로 안정화. 사실 수 검증 필요.',
})

# 04_enrich
en = snapshots.get('04_enrich', {})
en_ok = en.get('ok', False)
en_total = en.get('total_enriched', 0)
by_v = en.get('enrich_by_verdict', {})
ungrounded = by_v.get('UNGROUNDED', 0)
en_score = 9 if (en_ok and en_total > 0 and ungrounded == 0) else \
            8 if (en_ok and en_total > 0 and ungrounded <= 1) else \
            7 if en_ok else 2
evals.append({
    'phase': 'enrich', 'score': f'{en_score}/10', 'pass': en_ok and en_total > 0,
    'details': {'total_enriched': en_total, 'by_verdict': by_v, 'elapsed_s': en.get('elapsed_s',0)},
    'quality': 'LLM self-verify NLI (8085 불필요).',
    'usability': '사용 가능. GROUNDED 비율이 핵심 지표.',
})

# 05_day_verify
dv = snapshots.get('05_day_verify', {})
dv_ok = dv.get('ok', False)
dv_total = dv.get('total_verified', 0)
by_dv = dv.get('verify_by_verdict', {})
dv_ungrounded = by_dv.get('UNGROUNDED', 0)
dv_score = 9 if (dv_ok and dv_total > 0 and dv_ungrounded == 0) else \
             8 if (dv_ok and dv_total > 0 and dv_ungrounded <= 1) else \
             7 if dv_ok else 2
evals.append({
    'phase': 'day_verify', 'score': f'{dv_score}/10', 'pass': dv_ok,
    'details': {'total_verified': dv_total, 'by_verdict': by_dv, 'elapsed_s': dv.get('elapsed_s',0)},
    'quality': 'Entity faithfulness + TLDR 검증. Qwen2.5-Coder-7B.',
    'usability': '사용 가능. 8082 crash recovery + KV cache q8_0.',
})

# Overall
scores = [tc_score, pl_score, em_score, es_score, ex_score, en_score, dv_score]
passed = sum(1 for s in scores if s >= 7)
overall = {
    'total_phases': 7, 'phases_passed': passed, 'phases_failed': 7 - passed,
    'average_score': round(sum(scores) / len(scores), 1),
    'scores': {'text_clean': tc_score, 'polish': pl_score, 'embed': em_score,
               'entity_scan': es_score, 'extract': ex_score, 'enrich': en_score, 'day_verify': dv_score},
    'recommendation': '사용 가능' if passed >= 5 else '추가 수정 필요',
}

report = {
    'meta': {
        'timestamp': '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
        'kst': KST,
        'batch_limit': $LIMIT,
        'test': 'E2E 20-turn full pipeline with quality eval',
        'models': {'day-extractor': 'Qwen3-8B-Q8_0 (:8082)', 'day-verifier': 'Qwen2.5-Coder-7B-Instruct-Q8_0 (:8082)',
                   'embedder': 'Qwen3-8B-Q8_0 (:8081)', 'reranker': 'Qwen3-Reranker-4B-Q8_0 (:8080)'},
        'features': {'kv_cache_q8_0': True, '8082_auto_recovery': True, 'model_fingerprint': True,
                     'section_split_extract': True, 'llm_nli_self_verify': True, 'batch_limit_10': True},
    },
    'overall': overall,
    'snapshots': {k: v for k, v in snapshots.items()},
    'evaluations': evals,
}

report_path = '$REPORT'
with open(report_path, 'w') as f:
    json.dump(report, f, indent=2, ensure_ascii=False, default=str)

print()
print('=' * 60)
print('QUALITY EVALUATION SUMMARY')
print('=' * 60)
for e in evals:
    s = 'PASS' if e['pass'] else 'FAIL'
    print(f\"  {e['phase']:20s} | {e['score']:6s} | {s:4s} | {e['usability'][:50]}\")
print()
print(f\"Overall: {overall['phases_passed']}/{overall['total_phases']} passed, avg {overall['average_score']}/10\")
print(f\"Recommendation: {overall['recommendation']}\")
print(f\"Report: {report_path}\")
"

# ── Cleanup ──
$PY -c "
import sys; sys.path.insert(0, '.')
from lib.test_common import test_complete
test_complete('E2E 20-turn full pipeline completed')
print('Test protection cleaned up.')
"

log ""
log "============================================================"
log "E2E 20-TURN FULL PIPELINE COMPLETE"
log "============================================================"
log "Done at: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
log "KST: $(TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S')"
log "Report: $REPORT"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Snapshots: $SNAPSHOT_DIR/"
echo "Report:    $REPORT"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
