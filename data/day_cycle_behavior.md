# day_cycle.sh 행위 명세 (455줄)

## 1. 개요

`day_cycle.sh`은 systemd timer로 실행되는 **비동기 파이프라인 오케스트레이터**입니다.
- **총 예산**: 21,600초 (6시간)
- **실행 빈도**: 일 1회 (systemd timer `devforge-day-cycle.timer`)
- **상태 관리**: PostgreSQL `pipeline_state` 컬럼 (`pending → batching → cleaned → scanned → verified → enriched → embedded`)
- **중복 방지**: `flock`로 single-instance 보장 (`/tmp/devforge-day-cycle.lock`)

---

## 2. 행위 흐름 (4단계)

### 2-1. 시스템 동기화 (System Sync)

| 단계 | 설명 | 의존성 | 비고 |
|------|------|--------|------|
| **코드 구조 검사** | `gen_architecture.py --check-structure` | Python | exit code로 성공/실패 판단 |
| **DuckDNS 갱신** | `duckdns.org/update` API 호출 | HTTP, DUCKDNS_TOKEN | 실패 시 경고 로그 |
| **Watchdog liveness 확인** | `watchdog_liveness` 테이블 조회 | PostgreSQL, Podman | 3900초 이상 스텔이 시 Slack 알림 |
| **Worklog 생성** | `worklog_generator.py` 실행 | PostgreSQL, LLM(:8082) | 240초 timeout |

**예산 소진 시**: `BUDGET ≤ 120` → `exit 0` (조기 종료)

### 2-2. 배치 예약 (Batch Reservation)

```
in_flight = turns WHERE pipeline_state NOT IN ('pending', 'embedded', 'embed_skipped')

if in_flight == 0:
    reserve 50 turns from pending → batching (ORDER BY created_at ASC)
    return reserved_count
elif in_flight > 0:
    resume from existing pipeline_state
```

**DB 연산**: `podman exec postgres psql -U devforge -d devforge_app`
**예산**: BUDGET ≤ 120 → exit 0

### 2-3. 파이프라인 단계 (상태 전이)

| 단계 | Python 스크립트 | 상태 전이 | LLM 포트 | 예산 게이트 | 비고 |
|------|----------------|-----------|----------|-------------|------|
| **Text Clean** | `text_clean.py` | batching → cleaned | :8082 (day) | BUDGET > 60 | 언어 감지 + 틱톡으로 est_chars 설정 |
| **FTS5 Refresh** | `fts5_refresh.py` | (별도) | 없음 | BUDGET > 0 | 120초 timeout |
| **Entity Scan** | `entity_scan.py` | cleaned → scanned | 없음 | BUDGET > 60 | **LLM 없음**, regex + DB |
| **Extract** | `extract.py` | scanned → verified | :8082 | BUDGET > 60 + 배터리 게이트 | NLI self-verify 포함 |
| **Enrich** | `enrich.py` | verified → enriched | :8082 | BUDGET > 60 + 배터리 게이트 | grounding 포함 |
| **Embedding** | `embed_batch.py` | enriched → embedded | :8081 | BUDGET > 60 | 피드백 예제 병렬 |

#### 배터리 게이트 (`_budget_gate`)

```
파라미터: state, chars_per_sec, overhead_sec

if BUDGET < 120: skip (return 1)
if est_chars == 0: proceed (return 0)

est = est_chars / chars_per_sec + overhead

if state == "scanned":  # Extract 단계
    est = min(est, MAX_CYCLE_SEC / 2)  # 50% 캡

if BUDGET >= est: return 0 (proceed)
elif BUDGET >= 600: return 0 (partial, partial OK)
else: return 1 (defer)
```

### 2-4. Human-in-the-loop (NL 인터랙션)

| 단계 | 설명 | Slack/Telegram | 상태 처리 |
|------|------|----------------|------------|
| **Extract Fail Alert** | `extract_fail_report.json` 존재 시 | Slack 버튼 | - |
| **Noise Marker** | 사용자 확인 | Slack/Telegram | CONFIRM → verified, REJECT → delete |
| **NEUTRAL Auto-Resolve** | GROUNDED/UNGROUNDED 자동 처리 | 없음 | 24시간 이내, extract_pipeline 소스 |
| **NEUTRAL Gate** | AMBIGUOUS만 Slack 알림 | Slack | `exit 0` (사이클 중단) |

### 2-5. Reranker Recovery

| 단계 | 설명 | LLM 포트 | 비고 |
|------|------|----------|------|
| **Reranker Launch** | `:8080`에서 reranker 시작 | :8080 | `llama-server` 직접 실행 |
| **Recovery** | `RERANKER_ERROR` facts 재스코어 | :8080 | 600초 timeout |
| **Post-Extract Supplement** | 누락된 fact 보충 | :8082 | budget ≥ 600 → limit 10 |

---

## 3. 상태 전이 (State Machine)

```
pending ──(batch reserve 50)──→ batching
  ↓                                ↓
  (no-op)                    batching ──(text_clean)──→ cleaned
                                              ↓
                                            cleaned ──(entity_scan)──→ scanned
                                              ↓
                                            scanned ──(extract)──→ verified
                                              ↓
                                            verified ──(enrich)──→ enriched
                                              ↓
                                            enriched ──(embed)──→ embedded
                                              ↓
                                            embedded ──(embed_skip)──→ (완료)

in_flight = NOT IN (pending, embedded, embed_skipped)
```

**주의**: `extract.py`는 `scanned → verified+extracted` (2단계 동시)

---

## 4. 예산 관리 (Budget Manager)

```
MAX_CYCLE_SEC = 21600 (6시간)
START_TS = $(date +%s)

BUDGET() = MAX_CYCLE_SEC - (now - START_TS)

# 단계별 예산 체크 포인트:
- System Sync 이후: BUDGET ≤ 120 → exit
- Text Clean: BUDGET ≤ 60 → exit
- Entity Scan: BUDGET ≤ 60 → exit
- Extract: _budget_gate("scanned", 15, 120)
- Enrich: _budget_gate("verified", 20, 60)
- Embed: BUDGET ≤ 60 → exit (in-loop 체크)
- Feedback Embed: _budget_gate("enriched", 20, 30)
- Supplement: BUDGET ≥ 600 → limit 10
```

---

## 5. LLM 의존성 (현재 포트 기반)

| 단계 | 모델 | 포트 | 비고 |
|------|------|------|------|
| Text Clean | day-extractor | :8082 | est_chars 측정용 |
| Extract | day-extractor | :8082 | + NLI self-verify |
| Enrich | day-enricher | :8082 | grounding |
| Embedding | embeder | :8081 | Qwen3-Embedding-8B |
| Reranker | reranker | :8080 | Recovery 전용 |
| Worklog | reflector | :8082 | 300초 timeout |

**주의**: :8080과 :8081은 항상 활성 (reranker/embeder), :8082-8084는 day/verifier 모델 순환

---

## 6. Human-in-the-loop 상세

### 6-1. Noise Marker 처리
```sql
-- 사용자 CONFIRM → verified
UPDATE turns SET pipeline_state = 'verified'
FROM review_facts rf
WHERE rf.turn_id = turns.id
  AND rf.fact_type = 'noise_marker'
  AND rf.user_verdict = 'CONFIRM'
  AND turns.pipeline_state = 'scanned';

-- 사용자 REJECT → delete
DELETE FROM review_facts rf
USING turns
WHERE rf.turn_id = turns.id
  AND rf.fact_type = 'noise_marker'
  AND rf.user_verdict = 'REJECT'
  AND turns.pipeline_state = 'scanned';
```

### 6-2. NEUTRAL Auto-Resolve
```sql
-- GROUNDED 자동 처리 (24시간 이내)
UPDATE review_facts SET user_verdict = 'GROUNDED'
WHERE nli_llm = 'NEUTRAL' AND user_verdict IS NULL
  AND nli_verdict = 'GROUNDED'
  AND telegram_notified_at IS NULL
  AND created_at > now() - interval '24 hours'
  AND source = 'extract_pipeline';
```

### 6-3. NEUTRAL+AMBIGUOUS Gate
- `AMBIGUOUS` facts는 **Slack 알림 후 `exit 0`** (사이클 중단)
- 사용자 판결 대기 중

---

## 7. 행위 명세 작성 원칙

1. **결정론적 단계**: text_clean, entity_scan, FTS5 refresh, 배치 예약, Noise/NEUTRAL 처리
   - **출력이 동일해야** → diff = 0 검증 가능
2. **확률적 단계**: extract, enrich, verify, worklog
   - **LLM 응답이 다를 수 있음** → record/replay fixture로 결정론화
3. **상태 전이**: 모두 DB 트랜잭션으로 원자적
4. **예산 관리**: 모든 단계는 BUDGET() 체크 후 진행/중단 결정

---

## 8. record/replay 캡처 대상 (Phase −1)

| 단계 | 캡처 대상 | fixture 파일 |
|------|-----------|-------------|
| Batch Reservation | `SELECT ... FROM turns WHERE pipeline_state = 'pending' LIMIT 50` 결과 | `batch_reserve.sql` |
| Text Clean | `call_llm(model="reflector", ...)` 응답 | `text_clean_llm.json` |
| Extract | `call_llm(model="day_extract", ...)` 응답 (10건) | `extract_llm.json` |
| Enrich | `call_llm(model="day_enrich", ...)` 응답 (5건) | `enrich_llm.json` |
| NLI Verify | `_call_nli_server()` 응답 | `nli_result.json` |
| Reranker | `reranker_score()` 응답 | `rerank_score.json` |
| Worklog | `call_llm(model="reflector", ...)` 응답 | `worklog_llm.json` |

**총 6개 fixture 파일** — Phase −1에서 캡처 → Phase 3에서 replay 사용
