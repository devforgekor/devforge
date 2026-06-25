# Day/Night Pipeline Restructure — 서버 적용 검토 보고서

**작성**: 2026-06-10 KST  
**대상**: DEVFORGE (ARM Neoverse-N1, 4-core, 22Gi RAM)  
**검토 기준**: 제안된 피드백이 실제 서버 환경에 적용 가능한지 여부

---

## 1. 타이머 스케줄 충돌 — 수정 필요 ⚠️

### 피드백 내용
> `15m-cycle *:0/30`과 `day-verify *:30/30`이 **매시 30분**에 동시 실행된다.

### 서버 실측 결과
```
devforge-15m-cycle.timer:  OnCalendar=*:0/30  ← :00과 :30 양쪽에서 발화
```
15m-cycle 타이머는 **30분 간격**으로 설정되어 있어 **:00과 :30 모두**에서 발화합니다.  
제안된 대로 15m-cycle을 day_extract 용도로 유지하고 day_verify를 `*:30`에 추가하면:

| 시각 | 실행 | Pod |
|------|------|-----|
| :00 | 15m-cycle → day_extract (fast + extract + MCP) | Pod A (3B, 8082) |
| :30 | 15m-cycle → day_extract (또 한번!) | Pod A (3B) |
| :30 | day_verify | Pod B (14B) |

**:30에 Pod A와 Pod B가 동시에 실행 → 경합. 현재와 동일한 문제.**

### 적용: 수정
**타이머를 분리해야 합니다:**

| 타이머 | OnCalendar | 실행 | Pod |
|--------|------------|------|-----|
| `devforge-day-extract.timer` | `*:00` | fast tasks → extract → MCP enrich | Pod A (3B) |
| `devforge-day-verify.timer` | `*:30` | 14B verify + category | Pod B (14B) |

`devforge-15m-cycle.timer`는 **삭제** 또는 fast-only로 축소하고 `*:00` 전용 타이머로 교체.

현재 `15m_cycle.sh`가 이미 mode guard (`MODE=night` 체크)를 가지고 있으므로,  
day-extract 타이머에도 동일한 로직을 적용: `MODE=night`면 extract/verify 모두 SKIP.

---

## 2. 체크포인트 관리 — 이미 적절 (수정 불필요) ✅

### 피드백 내용
> extract와 mcp_enrich 체크포인트를 분리하지 말고 하나로 통합하라.

### 서버 실측 결과
```sql
pipeline_checkpoint:
  extract    | 2026-06-09 12:05:07+00
  mcp_enrich | 2026-05-14 15:05:49+00
  classify   | 2026-05-14 14:40:54+00
```
이미 **분리되어 있습니다** (`phase`가 PK). 그러나 현재 extract 체크포인트는 24시간 이상 갱신 안 됨 (= 백로그 189건).  
MCP 체크포인트는 5월 14일 (= 사실상 멈춤).

### 적용: 수정 불필요
- `pipeline_checkpoint`는 `phase``max_created_at` 구조로 이미 체크포인트별 분리 완료
- extract → mcp_enrich는 하나의 배치로 실행하되 **각각 체크포인트 갱신** (현재 구조 유지)
- 체크포인트 통합은 불필요 — 분리되어 있어야 mcp_enrich 실패 시 extract 재실행을 피할 수 있음

### 추가 발견: 체크포인트 갱신 로직 버그
현재 extract 체크포인트가 6월 9일 이후 갱신되지 않음.  
day_cycle.py가 extract → mcp_enrich 실행 후 체크포인트 갱신을 제대로 안 함.  
**day_extract.py에서 이 로직을 반드시 수정해야 함.**

---

## 3. eval/ 파일 Handover의 DB화 — Phase 2 과제 (보류) 🔶

### 피드백 내용
> night handover도 DB에 JSONB로 저장해야 완전한 SSOT.

### 서버 실측 결과
- `eval/`: 30개의 `pipeline_complete_*.json` (day_cycle.py 출력물),  
  21개 `pipeline_01_python_verify_*.json`, 12개 `eval_*.json`
- size: ~11KB 파일 각각 — 매우 작음
- night.py `_load_latest_phase()`가 `data/eval/pipeline_*.json`을 직접 읽음

### 적용: Phase 1에서는 보류
**이유:**
1. `pipeline_verify_*.json`은 단일 사이클 결과물 — 읽기/쓰기 모두 빈번하지 않음 (최대 1시간 1회)
2. eval/ 파일 구조를 night.py에서 깊게 의존 중 — 전환 시 night_review.py도 함께 수정 필요
3. 파일 partial write 리스크는 작음 (11KB, 쓰기 시간 < 100ms)
4. 완전한 DB 전환은 Phase 2 (night_review/night_verify 구현 시)로 연기

**Phase 1 규칙:** DB = SSOT for turn-level data, eval/ = cycle-level snapshots.  
**Phase 2 목표:** `night_handover` 테이블 추가 → eval/ 의존성 제거.

---

## 4. LLM 타임아웃 / SIGKILL — 이미 적용됨 (확인 완료) ✅

### 피드백 내용
> 스크립트에 hard timeout을 걸어야 함.

### 서버 실측 결과
```bash
# 15m_cycle.sh (line ~57)
timeout -k 10 "$BUDGET" python3 /opt/projects/server/pipelines/day_cycle.py
```
이미 `timeout -k 10` 적용 완료. BUDGET은 1500초 (25분).

### 적용: 그대로 유지
- day_extract.sh에도 동일한 `timeout -k 10 "$BUDGET"` 적용
- day_verify.sh에도 동일한 `timeout -k 10 "$BUDGET"` 적용
- BUDGET 값: extract 25분(1500s), verify 25분(1500s) — 버퍼 각 2분 포함

트랜잭션 ROLLBACK: `extract.py`와 `mcp_enrich.py`는 각 turn을 **개별 INSERT**하므로  
타임아웃으로 죽어도 부분 커밋이 완료됨 — 체크포인트만 갱신되지 않은 상태.  
다음 사이클에서 checkpoint 기준으로 재개 → 안전.

---

## 5. 기아(Starvation) 현상 — 대응 필요 ⚠️

### 피드백 내용
> defer가 N회 연속되면 백로그 폭증. catch-up 모드 필요.

### 서버 실측 결과
- extract 처리량 추정: Pod A 3B Q8, ~5.2 t/s  
  단, LLM 호출당 ~6초 가정 — 189건 백로그 ÷ 6s = **~1,134초 (19분)**
- 25분 예산 내에 189건 처리는 **가능하지만** :25 체크 시점에 남은 작업량이 3분 초과면 defer
- :25 체크가 extract 완료 후가 아니라 **:25 정각**에 실행된다는 점 중요

### 적용: 구현
```python
# day_extract.py buffer check
MAX_BUDGET = 1500     # 25분
BUFFER_MIN = 180       # 3분
연속_지연 = 0

while (remaining = MAX_BUDGET - elapsed) > BUFFER_MIN and backlog > 0:
    batch = get_next_batch()
    process_batch(batch)
    checkpoint.update()
    
if backlog > 0 and 연속_지연 >= 3:
    send_alert(f"extract backlog {backlog}건, {연속_지연}회 연속 미처리")
    # night mode에서 catch-up 실행 가능하지만: night_tasks != day_tasks
    # → 수동 개입 유도가 현실적
```

**Catch-up 모드는 보류.**  
이유: night 모드에서는 14B/30B/27B 모델이 Pod를 점유. day extract를 night에 끼워 넣으면  
야간 작업의 타이밀이 깨짐. 백로그가 지속되면 수동 개입이 더 현실적.

---

## 6. Model Warm-up / Unload 오버헤드 — 해당 없음 ❌

### 피드백 내용
> Pod A 종료 후 Pod B 시작 시 모델 로드/언로드 시간 고려.

### 서버 실측 결과
```
Pod A: devforge-pod-a :8082 (3B Q8) — Up 7 hours
Pod B: devforge-pod-b :8080~8081 (14B Q4) — Up 3 hours
```
두 Pod는 **별도 컨테이너**로 항상 실행 중.  
모델 언로드/로드가 필요 없음 — 메모리에 항상 상주.

### 적용: 해당 없음
그러나 **첫 추론 지연(Fist-inference latency)** 은 발생:
- Pod A (3B Q8): 첫 호출 ~17초 (warm-up)
- Pod B (14B Q4): 첫 호출 ~70초 (warm-up)

이는 `day_extract.py` / `day_verify.py` 시작 시 첫 LLM 호출에만 발생.  
버퍼 계산에 포함할 필요는 없음 (25분 예산 내에서 무시 가능한 수준).

---

## 7. day_verify 배치 크기 — 적용 ✅

### 피드백 내용
> day_verify에도 limit이 필요.

### 적용: 구현
- `day_extract.py`: `--limit 50` (유지)
- `day_verify.py`: `--limit 50` (추가)
- verify 처리량 추정: 14B Q4_K_M ~1.5 t/s  
  50건 × ~15초 = ~750초 (12.5분) — 25분 예산 내 충분

---

## 8. DB 트랜잭션 격리 — 해당 없음 ❌

### 피드백 내용
> Pod A의 미커밋 데이터를 Pod B가 읽을 위험. Safety delay 필요.

### 서버 실측 결과
```sql
SHOW transaction_isolation; -- read committed
```

### 적용: 해당 없음
- Pod A (3B)는 WRITE만 수행. Pod B (14B)는 READ + WRITE (verify 결과).
- `day_extract`가 쓴 `review_facts`는 즉시 커밋 (각 turn별 개별 INSERT + checkpoint UPDATE)
- `:30`에 Pod B 시작 시 `:28`에 Pod A 작업이 이미 커밋 완료 (2분 버퍼)
- `READ COMMITTED` 수준에서 이미 커밋된 데이터만 읽음 → 문제 없음

Safety delay 불필요. 버퍼 2분이 충분한 간격.

---

## 9. Systemd 타이머 정밀도 — 적용 ✅

### 피드백 내용
> `AccuracySec=1s` 및 `RandomizedDelaySec=0` 설정 권장.

### 서버 실측 결과
```ini
[Timer]
OnCalendar=*:0/30
RandomizedDelaySec=5   ← 5초 랜덤 지연 있음
```

### 적용: 수정
**day-extract**와 **day-verify** 타이머:
```ini
[Timer]
OnCalendar=*:00
AccuracySec=1s
RandomizedDelaySec=0
```
5초 지연은 30분 슬롯에 영향 없으나, 정확한 `:00`/:30 발화를 위해 제거.

---

## 10. 오류 처리 / 재시도 한계 — 적용 필요 ⚠️

### 피드백 내용
> 3회 연속 실패 시 알림. 영원한 재시도 방지.

### 서버 실측 결과
- extract 백로그 189건 — 이미 한 번 멈춤. 실패 검출 로직 부재.
- MCP 체크포인트 5월 14일 — 4주간 멈춤. 아무도 모름.

### 적용: 구현
```python
실패_누적 = 0
MAX_RETRY = 3

for turn in batch:
    try:
        process(turn)
        실패_누적 = 0
    except:
        실패_누적 += 1
        if 실패_누적 >= MAX_RETRY:
            send_alert(f"day_extract: {turn.id} 연속 실패")
            checkpoint.advance(실패_지점_다음)  # 무한 재시도 방지
            실패_누적 = 0
```

알림 대상: Telegram or worklog DB (`worklog_entries`).

---

## 11. eval/ 파일명 규칙 — 적용 ✅

### 피드백 내용
> 파일명에 날짜/시간 포함해 덮어쓰기 방지.

### 서버 실측 결과
현재 `pipeline_complete_20260609T053528Z.json` 형식 — 이미 timestamp 포함되어 있음.

### 적용: 규칙 유지
- `pipeline_verify_20260610T003000Z.json` (사이클 타임스탬프)
- `pipeline_extract_20260610T000000Z.json` (사이클 타임스탬프)

기존 형식 그대로 사용. day_verify만 `pipeline_verify_` 접두사로 저장.

---

## 12. 현재 백로그 현황 — 운영 영향 있음 📊

| 단계 | 대기 건수 | 마지막 체크포인트 | 소요 시간 예상 |
|------|-----------|-------------------|---------------|
| extract 필요 | 189 | Jun 9 12:05 UTC | ~19분 (3B) |
| MCP enrich 필요 | 38 | May 14 | ~4분 (3B) |
| 14B verify 필요 | 229 | (일부만 진행) | ~57분 (14B) |

verify 백로그 229건이 가장 큼. `--limit 50` 기준 5사이클 (2.5시간) 소요.  
**초기 실행 시 verify 백로그 해소 시간이 필요함을 인지해야 함.**

---

## 요약: 적용 결정

| # | 피드백 항목 | 적용 | 비고 |
|---|-----------|------|------|
| 1 | Timer 스케줄 충돌 | **수정** | `*:0/30` 분리 → `*:00` + `*:30` |
| 2 | 체크포인트 통합 | 불필요 | 이미 분리되어 있음. 갱신 버그만 수정 |
| 3 | eval/ → DB 전환 | **Phase 2** | night_review 구현 시 병행 |
| 4 | Hard timeout | 이미 있음 | `timeout -k 10` 유지 |
| 5 | 기아(Starvation) 방지 | **구현** | 연속 3회 지연 → 알림 |
| 6 | Model warm-up | 해당 없음 | 별도 컨테이너, 항상 실행 중 |
| 7 | day_verify limit | **적용** | `--limit 50` |
| 8 | TX 격리 Safety delay | 해당 없음 | 2분 버퍼 + READ COMMITTED |
| 9 | Timer 정밀도 | **적용** | `AccuracySec=1s`, `RandomizedDelaySec=0` |
| 10 | 재시도 한계/알림 | **구현** | 3회 연속 실패 → 체크포인트 advance |
| 11 | eval/ 파일명 | 이미 적절 | timestamp 포함 형식 유지 |
| 12 | verify 백로그 229건 | **운영 고려** | 초기 2.5시간 소요 예상 |

**적용: 7/12 항목. 보류: 2항목. 불필요/기존: 3항목.**
