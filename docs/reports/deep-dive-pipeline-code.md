# Deep Dive: Pipeline Code Analysis

> 2026-07-22 — All pipeline scripts (scripts/pipelines/) reviewed against current day_cycle.sh flow
> Updated 2026-07-27: nd13 fixes, enrich 듀얼→단일, embed_batch overlap + 전처리

## 1. Pipeline File Inventory (28 scripts)

### 1.1 Production Status (14 scripts)

| File | Lines | Path (callers) | Dependencies |
|------|-------|---------------|-------------|
| `extract.py` | ~1,090 | day_cycle.sh → subprocess | extract_llm.py, extract_verify.py, lib.pod_manager, lib.watchdog.checker |
| `extract_llm.py` | ~296 | extract.py — submodule | lib.llm_client, lib.model_registry, lib.pod_manager |
| `extract_verify.py` | ~400 | extract.py — submodule | lib.llm_client (call_llm, reranker_nli_verdict) |
| `embed_batch.py` | ~400 | day_cycle.sh → subprocess | lib.pod_manager, lib.infra.preflight |
| `fts5_refresh.py` | ~70 | day_cycle.sh → subprocess | lib.search.local_index |
| `reranker_recover.py` | ~160 | day_cycle.sh → subprocess | lib.llm_client |
| `text_clean.py` | ~340 | day_cycle.sh → subprocess | lib.text_cleaner |
| `review_consumer.py` | ~400 | day_cycle.sh → subprocess | lib.llm_client |
| `proxy_reviewer.py` | ~450 | night_cycle.sh → subprocess | lib.llm_client, lib.notify |
| `worklog_generator.py` | ~430 | day_cycle.sh → subprocess | lib.db, lib.notify |
| `prj_cycle.py` | ~150 | systemd timer | lib.db |
| `night_debate_pipeline.py` | ~200 | night_cycle.py | lib.pipeline_common |
| `review.py` | ~100 | n/a | lib.db |

### 1.2 Experimental Status (9 scripts)

| File | Lines | Path | Notes |
|------|-------|------|-------|
| `enrich.py` | ~1,200 | day_cycle.sh → subprocess | preflight → (model: day_cycle.sh ensure_inference) |
| `night_cycle.py` | ~400 | night_cycle.sh → subprocess | P-R-J debate pipeline |
| `night_cycle_pipeline.py` | ~100 | n/a | Experimental night pipeline |
| `day_verify.py` | ~640 | **NOT CALLED** | Dead code — no caller in day_cycle.sh |
| `entity_scan.py` | ~500 | day_cycle.sh → subprocess | Pattern-based entity extraction |
| `post_extract_supplement.py` | ~400 | day_cycle.sh → subprocess | Uses extract_llm |
| `raw_consumer.py` | ~110 | turn_watcher → subprocess | Polls for raw → pending |
| `review_facts_dedupe.py` | ~150 | n/a | Deduplication utility |

### 1.3 Deprecated Status (1 script)

| File | Lines | Notes |
|------|-------|-------|
| `polish_batch.py` | ~360 | Merged into text_clean.py. Should be archived. |

### 1.4 Key Finding: All Pipeline Scripts Are Already Python

**Every pipeline script is already Python.** Zero bash pipeline scripts. The only bash remaining
is orchestration (day_cycle.sh, night_cycle.sh) and library (model_ctl.sh).

---

## 2. Pipeline Data Flow (End-to-End)

```
turn_watcher (jsonl → DB, raw insert)
  → raw_consumer (raw → clean → pending)
  → day_cycle.sh orchestrates:
      ├─ text_clean.py        (pending → cleaned)
      ├─ fts5_refresh.py      (cleaned → scanned via FTS5)
      ├─ entity_scan.py       (scanned, deterministic entities)
      ├─ extract.py           (scanned → extracted+verified)
      │   ├─ _preflight_gate()
      │   ├─ _launch_reranker()
      │   ├─ Phase 1: extract (section-major KV cache batch)
      │   ├─ Phase 2: _llm_nli_verify (NLI self-verify v1)
      │   ├─ Phase 3: _llm_nli_verify2 (NLI self-verify v2)
      │   └─ Phase 4: _quality_check_facts + DB store
      ├─ reranker_recover.py  (re-score RERANKER_ERROR facts)
      ├─ post_extract_supplement.py (offline missing-fact LLM)
      ├─ enrich.py            (verified → enriched)
      │   ├─ preflight_checks()
      │   ├─ (model 기동: day_cycle.sh ensure_inference → 단일 day-enricher Q8_0)
      │   ├─ ThreadPool 단일 모델 parallel=2
      │   ├─ TLDR NLI verify + entity grounding
      │   └─ DB store (review_facts + pipeline_state)
      └─ embed_batch.py       (enriched → embedded)
          ├─ orphan cleanup
          ├─ preflight_checks + ensure_model
          └─ dynamic batching
```

### 2.1 pipeline_state Transitions

```
raw ──[raw_consumer]──→ pending
pending ──[day_cycle: text_clean]──→ batching
batching ──[day_cycle: text_clean]──→ cleaned
cleaned ──[day_cycle: fts5+entity]──→ scanned
scanned ──[day_cycle: extract.py]──→ extracted+verified
extracted+verified ──[day_cycle: enrich.py]──→ enriched
enriched ──[day_cycle: embed_batch.py]──→ embedded
```

Note: `day_verify.py` formerly handled `extracted → verified` but this was merged into
`extract.py` Phase 2-3. The state `extracted+verified` is a single state.

---

## 3. extract.py Self-Sufficiency (Critical Finding)

### 3.1 What extract.py Handles Independently

| Function | Details |
|----------|---------|
| **Preflight gate** (`_preflight_gate()`) | Model file check, memory budget (4GB or 12GB), :8082 health + auto-restart, :8080 reranker + auto-launch, DB connectivity |
| **Model pod start** | `_ensure_model_pod("day-extractor", skip_if_healthy=True)` |
| **Reranker launch** (`_launch_reranker()`) | Same pattern as day_cycle.sh version |
| **NLI verify v1** | `_llm_nli_verify` in extract_verify.py |
| **NLI verify v2** | `_llm_nli_verify2` — second pass with corrected evidence |
| **Quality check** | `_quality_check_facts` — fact-level quality scoring |
| **8082 auto-recovery** | `_call_with_8082_retry` in extract_llm.py — connection error detection + recovery |
| **Column migration** | `ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS` for `nli_llm2`, `quality_checks` |

### 3.2 What day_cycle.sh Still Does Around extract.py

```
day_cycle.sh lines 274-316:
  ├── Budget gate (NEED_EXTRACT: pipeline_state='scanned')
  ├── echo "=== Day Extract ==="
  ├── python3 extract.py                        ← extract.py handles everything
  ├── Noise marker (UPDATE turns SET pipeline_state)
  ├── NEUTRAL auto-resolve (4-hour inactivity)
  ├── Reranker recovery: python3 reranker_recover.py
  └── Post-extract supplement: python3 post_extract_supplement.py
```

The bash layer around extract.py is **6 operations**, all of which are:
- Simple DB queries (COUNT, UPDATE)
- python3 subprocess calls
- curl to Slack

---

## 4. enrich.py Analysis

### 4.1 Key Architecture

| Aspect | Detail |
|--------|--------|
| Status | experimental |
| Lines | ~1,200 |
| Model start | **day_cycle.sh가 `ensure_inference("day-enrich", ...)`로 단일 Q8_0 모델 기동** (2026-07-24 변경: 듀얼 Q4_K_M → 단일 Q8_0) |
| LLM strategy | ThreadPool 단일 모델, parallel=2 |
| Short turn optimization | < SHORT_TURN_THRESHOLD → single-token intent classification (no LLM) |
| Solo turns | > MAX_CHARS_SOLO (5000) → separate pool, longer timeout |
| TLDR NLI | `_verify_tldr` → CONTRADICTION → `_fix_contradiction_tldr` retry → source fallback |
| Entity grounding | `_verify_entities` — rejects hallucinated entities |
| DB storage | `_insert_enrich_fact` — JSONB enrich_data in review_facts |

### 4.2 Bash Independence

enrich.py is **fully independent** of bash:
- No subprocess calls
- All DB via lib.db
- All model management via lib.pod_manager
- No python3 -c inline needed

---

## 5. embed_batch.py Analysis

### 5.1 Key Architecture

| Aspect | Detail |
|--------|--------|
| Status | production |
| Lines | ~400 |
| Model start | `preflight_checks()` + `ensure_model("embeder", skip_if_healthy=True)` |
| Prerun cleanup | Orphaned embedding chunk removal (DELETE FROM embeddings WHERE turn missing) |
| Fallback | :8080 primary, :8081 fallback if inference unavailable |
| Dynamic batching | Token-budget-based per batch (SLOT_CTX ~3300 tok) |
| Modes | Turns (default), Facts (--facts), Feedback (--feedback) |

### 5.2 Bash Independence

Fully independent. Single call: `python3 embed_batch.py` (plus --facts, --feedback flags).

### 5.3 Recent Changes (2026-07-24)

| 항목 | 이전 | 이후 |
|------|------|------|
| 텍스트 전처리 | 없음 | NFKC 정규화 + 공백 축소 (`preprocess_for_embed()`) |
| 청크 오버랩 | 없음 | 64토큰 오버랩 (`_get_tail_sentences()`) |
| text_clean fallback | `COALESCE(t.text_clean, t.text_clean_polished)` | `+ t.text` (raw text fallback) |
| 최소 길이 | IS NOT NULL | `LENGTH >= 15` |
| 중복키 | `(source_type, source_id, model_name)` | `+ chunk_index` (청크 단위 upsert) |
| 짧은 청크 (<15자) | skip 처리 없음 | 청크 생성 후 skip |

---

## 6. Dead Code Discovery

### 6.1 day_verify.py — No Longer Called

| Evidence | Detail |
|----------|--------|
| Status | `experimental` (was `production` before pipeline merge) |
| day_cycle.sh call | Removed — no `python3 day_verify.py` in current day_cycle.sh |
| Responsibility | Predicate NLI with Veritas-8B — now handled by extract.py `_llm_nli_verify` |
| _launch_reranker() | 3rd copy! Same pattern as day_cycle.sh and extract.py |
| Size | ~640 lines |
| **Recommendation** | Archive to `_archive/pipelines/day_verify.py` |

### 6.2 polish_batch.py — Deprecated

| Evidence | Detail |
|----------|--------|
| Status | `deprecated` |
| Merged into | text_clean.py (unified preprocessing) |
| Size | ~360 lines |
| **Recommendation** | Archive to `_archive/pipelines/polish_batch.py` |

### 6.3 _launch_reranker() — Triple Duplication

| Location | Lines | Status |
|----------|-------|--------|
| day_cycle.sh | 140-166 | ACTIVE (but redundant) |
| extract.py | 1050-1081 | ACTIVE (preferred) |
| day_verify.py | ~551 | DEAD (archived with day_verify.py) |

Since extract.py `_preflight_gate()` handles reranker launch, day_cycle.sh's version is
effectively dead code. However, the day_cycle.sh version runs *before* extract.py, so it
can still be triggered if the reranker isn't already running when day_cycle.sh reaches
the extract.py call. The `_preflight_gate()` in extract.py is the safety net.

---

## 7. Remaining Bash Analysis (Priority Re-ranking)

### 7.1 Bash Script Scoring (Updated with Deep Dive Findings)

```
Scoring: DB=*2, py3c=*3, curl=*1, funcs=*1, subprocess python3=*1

Script                 Lines  DB py3c curl func sub   Score  Priority
─────────────────────────────────────────────────────────────────────
day_cycle.sh            462   19   2   3    5    14    77    HIGH
night_cycle.sh          245    0   3   1    6     4    16    HIGH
model_ctl.sh            240    0   0   0   10     0    10    HIGH (lib)
run_baseline_monitor.sh  83    1   4   0    0     1    15    MED
weekly_enrich_rebuild.sh 57    0   2   0    0     2     8    LOW
system_sync.sh           42    0   0   1    0     2     3    EXCLUDED
auto_mode.sh            271    0   0   0    4     0     4    LOW
─────────────────────────────────────────────────────────────────────
```

Note: I added `sub` (subprocess call to python3 pipeline) as a scoring factor (=1 each)
since each call represents a bash reliability risk (timeout, error propagation) even though
the called script is already Python. day_cycle.sh has 14 subprocess calls.

### 7.2 Subprocess Safety Risk in day_cycle.sh

Every `python3 scripts/pipelines/xxx.py` in day_cycle.sh lacks timeout:
```bash
python3 "$PIPELINE_DIR/extract.py" 2>&1          # no timeout!
python3 "$PIPELINE_DIR/enrich.py" 2>&1            # no timeout!
python3 "$PIPELINE_DIR/embed_batch.py" 2>&1       # no timeout!
```

Bash relies on systemd `TimeoutSec` but systemd sends SIGTERM to the bash process, not to
the python3 subprocess directly. If python3 hangs, systemd kills bash but python3 becomes
orphaned. All pipeline scripts should handle their own timeouts (embed_batch already does
via `_liveness_heartbeat`, extract.py has internal timeouts via LLM client)

---

## 8. Recommendations

### 8.1 Immediate (Low Effort, High Impact)

1. **Archive dead code**: `day_verify.py` → `_archive/pipelines/`, `polish_batch.py` → `_archive/pipelines/`
2. **Document _launch_reranker() redundancy**: Day_cycle.sh version is backup, extract.py version is primary

### 8.2 Short-term (Phase 0)

3. **model_ctl.sh → Python** (1-2h): The only bash library remaining. All pipeline scripts
   depend on it. After migration, every pipeline becomes fully bash-independent.

### 8.3 Medium-term

4. **day_cycle.sh → Python** (deferred, ROI reduced): 77 score but 14/14 subprocess calls
   are to already-Python scripts. Primary value is:
   - Consistent timeout handling (no orphaned python3 processes)
   - DB query unification (psql_json instead of raw SQL)
   - Budget gate integration with pipeline_state

5. **run_baseline_monitor.sh → Python** (0.5-1h): Highest python3-c density (4/83L)

### 8.4 Not Recommended

6. **night_cycle.sh**: Keep bash — the model switching logic (mode transition, inference
   swap, EXIT trap) is inherently shell-dependent and clearly correct in bash
7. **system_sync.sh, entrypoints, open-newhand.sh**: No migration benefit

### 8.5 Key Risk: subprocess Timeout

The single most important issue is **missing subprocess timeouts**. If this warrants
migration, the priority is day_cycle.sh (14 unprotected subprocess calls). A minimal fix
without full migration is to add `timeout` in bash:

```bash
timeout 600 python3 "$PIPELINE_DIR/extract.py" 2>&1 || true
```

---

## 9. Post-v2.0 Changes (2026-07-24 ~ 2026-07-27)

### 9.1 enrich.py: Dual Model → Single Q8_0

| 항목 | 이전 | 이후 |
|------|------|------|
| 모델 구성 | day-enricher (Q4_K_M, 4.7GB) + day-enricher-b (Q4_K_M, 4.7GB) | day-enricher (Q8_0, **8.2GB**) **단일** |
| threads/threads_batch | 2 | **4** |
| CPU cores | 0-1 | **0-3** (전 코어) |
| flash_attn | 없음 | 1 (활성화) |
| 모델 기동 위치 | enrich.py 내부 `ensure_sequential_dual()` | **day_cycle.sh `ensure_inference()`** |
| 종료 cleanup | `_cleanup_all_llms()` (전체 종료) | `_cleanup_all_llms(keep_8082=True)` (8082 유지) |
| 영향 | 듀얼 모델 A/B round-robin 제거 | ThreadPool parallel=2 동일 모델 |

### 9.2 embed_batch.py: Chunk Overlap + Preprocessing

- **NFKC 정규화 + 공백 축소** (`preprocess_for_embed()`)
- **64토큰 오버랩** 청킹 (`_get_tail_sentences()`)
- `text_clean` → `text_clean_polished` → **raw `text` fallback** (3단계 폴백)
- 중복키: `(source_type, source_id, model_name)` → **`+ chunk_index`** (청크 단위 upsert)
- 최소 길이: `IS NOT NULL` → **`LENGTH >= 15`** (15자 미만 skip)

### 9.3 extract_verify.py: Batch NLI Token-Budget Split

nd13-03 fix — 60 facts HTTP 500 overflow 재발 방지:

- **MAX_BUDGET=7373** (8192 ctx × 0.9) 기준 동적 배치 분할
- 각 fact evidence: 500ch 고정 → `context_limit(400)` (샌드위치 200+200)
- 각 fact 추정: `len(ev)//3 + 25`, 누적 시 분할
- **출처 truncation**: 2000ch → 1000ch

### 9.4 랭커/점수 truncation 2000→1500 일괄 변경

| 함수 | 이전 | 이후 |
|------|------|------|
| `llm_client.reranker_score()` | query/document 2000 | 1500 |
| `extract_verify._rerank_score()` | evidence/source 2000 | 1500 |

### 9.5 extract.py --large-only 플래그

- `_get_unprocessed_turns(large_only)` 파라미터 추가
- SQL: `AND (LENGTH(user_turn) > 2000 OR LENGTH(text) > 2000)`
- 용도: 대형 identity 문서 분리 처리
- pipeline_state `IN (scanned, pending)` — nd13-04 fix

### 9.6 Reranker 안정화 (nd13-01 ~ nd13-06)

| ID | Fix | 영향 |
|----|-----|-------|
| nd13-01 | batch-size 256→1024, ctx 2048→4096 | Reranker HTTP 500 제거 |
| nd13-02 | pkill+relaunch | Stale port config 방지 |
| nd13-03 | MAX_BUDGET=7373 동적 분할 | Batch NLI overflow 방지 |
| nd13-04 | pipeline_state IN (scanned, pending) | 5015 pending 누락 복구 |
| nd13-05 | 40-turn regression v4 | 0 errors, 165 facts, ~3h 50m |
| nd13-06 | Swap 분석, batch-size 1024 safe | OOM 리스크 없음 |
