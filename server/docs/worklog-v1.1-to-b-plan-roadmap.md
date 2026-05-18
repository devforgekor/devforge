# Worklog Auto-Reconciliation: v1.1 → B-Plan 진화 로드맵

**최종 결정**: v1.1 즉시 배포 + 2주 모니터링 + 안정성 증명 → B-Plan 전환

---

## 📋 전체 타임라인

```
┌─ PHASE 1: v1.1 Immediate Deployment (이번 주)
│  ├─ Duration: 1-2 hours
│  ├─ Action: session_guard.py에 따옴표 버그 수정 + DB UNIQUE constraint 추가
│  ├─ Target: 프로덕션 배포
│  └─ Success Metric: 연속 100% 성공 기록 (따옴표 오류 0건)
│
├─ PHASE 2: Monitoring & Stability Proof (1-2주)
│  ├─ Duration: 14 days
│  ├─ Action: 일일 worklog 기록율 모니터링 (100% 달성 확인)
│  ├─ Target: systemd timer 정상 작동 + DB 안정성 확인
│  └─ Success Metric: 
│      • 총 커밋 스캔율 98%+ (미스 2% 이내)
│      • DB 중복 오염 0건
│      • systemd 자동 재시도 정상 작동
│
├─ PHASE 3: B-Plan Environment Validation (다음주)
│  ├─ Duration: 30-60 minutes (Phase 0 of B-Plan)
│  ├─ Action: 5가지 환경 검증 실행
│  │   1. Git repository validate
│  │   2. PostgreSQL connectivity + schema
│  │   3. worklog_entries 테이블 구조 확인
│  │   4. Local LLM (localhost:4000 LiteLLM) 연결 테스트
│  │   5. systemd timer 설정 재검증
│  ├─ Target: B-Plan 구현 준비
│  └─ Success Metric: 5/5 체크 패스
│
├─ PHASE 4: B-Plan Code Implementation (다음 주-다음 달)
│  ├─ Duration: 4.5 hours (AI 에이전트 작성 기준)
│  ├─ Module: worklog_config.py, git_scanner.py, llm_caller.py, db_saver.py, etc.
│  ├─ Target: 스테이징 환경 배포
│  └─ Success Metric: 모든 Phase 1-3 유닛 테스트 통과
│
├─ PHASE 5: B-Plan Staging Tests (다음 달)
│  ├─ Duration: 45 minutes (통합 테스트 + 성능 테스트)
│  ├─ Action: 유닛 테스트 + 통합 테스트 + 부하 테스트
│  ├─ Target: 스테이징에서 100% 안정성 확인
│  └─ Success Metric: 300 커밋 × LLM 호출 모두 성공 + 타임아웃 0건
│
└─ PHASE 6: B-Plan Production Cutover (다음 달 중순)
   ├─ Duration: 2-3 hours (마이그레이션 + 헬스 체크)
   ├─ Action: v1.1 → B-Plan 전환 + 기존 로그 마이그레이션
   ├─ Target: 프로덕션 운영
   └─ Success Metric: 24시간 에러율 < 1%
```

---

## 🎯 PHASE 1: v1.1 즉시 배포

### 작업 항목

#### 1.1 session_guard.py — SQL 인젝션 방지 + commit 로깅 추가
```python
# review_worker.py:165-167 참조 — DB 문자열 이스케이프
def _esc_sql(s: str) -> str:
    return s.replace("'", "''").replace("\\", "\\\\")

# commit 메시지 삽입 시 반드시 이스케이프 적용
sha = _esc_sql(sha)
message = _esc_sql(message[:100])
sql = f"INSERT INTO worklog_entries (date, title, git_commit_hash, summary, agent) ..."
# commit message에 single quote(')가 들어가도 안전
```

#### 1.2 DB Schema 변경 (commit_hash 컬럼 + UNIQUE constraint)
```sql
-- Step 1: 컬럼 추가 (없는 경우에만)
ALTER TABLE worklog_entries ADD COLUMN IF NOT EXISTS git_commit_hash TEXT;

-- Step 2: 중복 방지 UNIQUE constraint
ALTER TABLE worklog_entries ADD UNIQUE (date, git_commit_hash);

-- Step 3: 부분 인덱스 (NULL 허용 — 수동 기록용)
CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_commit_hash
  ON worklog_entries(git_commit_hash)
  WHERE git_commit_hash IS NOT NULL;
```

#### 1.3 systemd 환경변수 설정
```ini
# 현재 운영 중인 타이머:
#   review-worker.timer → review-worker.service (review_worker.py)
#   devforge-qwen-worker.timer → devforge-qwen-worker.service (구형, 폐기 예정)
# v1.1 적용 대상: session_guard.py 호출 방식에 따라 적절한 서비스에 적용

[Service]
SuccessExitStatus=0
Restart=on-failure
RestartForceExitStatus=1
RestartMaxAttempts=3
RestartSec=60
StandardOutput=journal
StandardError=journal
```
주의: `SuccessExitStatus=2`는 Lock collision skip을 성공으로 오인할 수 있어 제외.

### 배포 명령어
```bash
# 1. 수정 적용
cd /opt/projects/server
git add scripts/session_guard.py
git commit -m "fix: add git_commit_hash column, SQL escaping, commit logging"

# 2. DB 마이그레이션 (podman exec 사용)
podman exec -i postgres psql -U postgres -d devforge_app << 'SQL'
ALTER TABLE worklog_entries ADD COLUMN IF NOT EXISTS git_commit_hash TEXT;
ALTER TABLE worklog_entries ADD UNIQUE (date, git_commit_hash);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wl_commit_hash ON worklog_entries(git_commit_hash) WHERE git_commit_hash IS NOT NULL;
SQL

# 3. systemd 재로드 & 서비스 재시작
systemctl --user daemon-reload
systemctl --user restart review-worker.service
```

---

## 📊 PHASE 2: 모니터링 & 안정성 증명 (2주)

### 매일 확인할 메트릭

#### 일일 체크리스트 (KST 오전 9시)
```bash
# 어제 커밋 기록 확인
podman exec -i postgres psql -U postgres -d devforge_app -c \
  "SELECT COUNT(*), COUNT(DISTINCT git_commit_hash) FROM worklog_entries \
   WHERE date = CURRENT_DATE - INTERVAL '1 day';"

# 에러 로그 확인
journalctl --user -u review-worker.service -p err --since "24 hours ago"

# 중복 기록 확인
podman exec -i postgres psql -U postgres -d devforge_app -c \
  "SELECT git_commit_hash, COUNT(*) FROM worklog_entries \
   GROUP BY git_commit_hash HAVING COUNT(*) > 1;"
```

#### 성공 기준
| 메트릭 | 목표 | 주간 리포트 |
|------|------|----------|
| 커밋 기록율 | 98%+ | 월요~금요 일일 기록 5/5 이상 |
| 중복 오염 | 0건 | 주간 중복 기록 0건 |
| 에러 복구율 | 100% | systemd 재시도 성공 3회 이상 |

### 주간 리포트 (금요일 5시)
```yaml
week_1_monitoring:
  total_commits_scanned: 127
  commits_logged: 125
  miss_rate: 1.6%
  duplicate_records: 0
  errors_recovered: 2
  systemd_restart_count: 1
  status: "PASS - 안정성 확인"
```

---

## ⚠️ PHASE 2에서 발견될 수 있는 이슈 & 대응

### 잠재적 문제 1: Quote Escaping 미흡
```
증상: 한글/특수문자 포함 커밋 메시지 기록 실패
대응: SQL 파라미터화 또는 json.dumps() 사용
```

### 잠재적 문제 2: DB 동시성 충돌
```
증상: 같은 커밋이 두 번 기록됨
대응: UNIQUE constraint 작동 확인 + 애플리케이션 레벨 dedup
```

### 잠재적 문제 3: systemd Timer 오차
```
증상: 15분 타이머가 실제로는 20분 이상 간격
대응: journalctl 타임스탬프 분석 + 정확도 검증
```

---

## 🔧 PHASE 3: B-Plan 환경 검증

### P0-1: Git Repository 검증
```bash
# DevForge 서버의 git log 접근성 확인
cd /opt/projects/server
git log --oneline --since="24 hours ago" | wc -l
# 예상: 최소 5개 이상 커밋
```

### P0-2: PostgreSQL 연결 + 스키마
```bash
# DB 접근성 + worklog_entries 테이블 확인
podman exec -i postgres psql -U postgres -d devforge_app -c "\d worklog_entries;"
# 예상: columns: id, date, git_commit_hash, title, agent, created_at
```

### P0-3: worklog_entries 스키마 검증
```sql
-- B-Plan에서 필요한 열 확인
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'worklog_entries'
ORDER BY ordinal_position;

-- 추가 필요한 열이 있는지 확인
-- B-Plan에서 필요: git_commit_hash (UNIQUE INDEX), llm_summary (nullable)
```

### P0-4: Local LLM 연결 테스트
```bash
# LiteLLM 라우터 (localhost:4000) 헬스 체크 — LLM 호출은 반드시 이 엔드포인트 사용
curl -s http://localhost:4000/health | jq .

# llama.cpp 백엔드 (localhost:8080) 헬스 체크
curl -s http://localhost:8080/health | jq .

# 간단한 API 콜 테스트 (timeout 30s)
curl -X POST http://localhost:4000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer devforge-litellm-key" \
  -d '{
    "model": "qwen2.5-coder-14b",
    "messages": [{"role": "user", "content": "Test"}],
    "max_tokens": 100
  }' \
  --max-time 30
```

### P0-5: systemd Timer 재검증
```bash
# 기존 타이머 확인
systemctl --user list-timers review* devforge*

# 기존 타이머가 15분 주기인지 확인
systemctl --user cat review-worker.timer

# B-Plan용 새 타이머 생성 가능성 검토
# (관심사 분리: v1.1은 기존 타이머, B-Plan은 별도 타이머)
```

### PHASE 3 성공 기준
```yaml
p0_validation_checklist:
  - git_log_access: ✅ 20 commits found
  - postgresql_connected: ✅ worklog_entries table exists
  - schema_validated: ✅ 7 columns matched
  - llm_healthcheck: ✅ response_time < 10s
  - timer_verified: ✅ 15-minute interval confirmed
  status: "READY_FOR_PHASE_4"
```

---

## 🛠️ PHASE 4: B-Plan 코드 구현

### 모듈 분할 (AI 에이전트 작성 대상)

| 모듈 | 파일 | 라인 수 | 역할 | 의존 |
|------|-----|-------|------|------|
| Config | worklog_config.py | 150 | 환경 설정 로드 | None |
| Git Scanner | git_scanner.py | 200 | git log 파싱 | Config |
| LLM Caller | llm_caller.py | 250 | LocalLLM API 호출 | Config |
| DB Saver | db_saver.py | 200 | PostgreSQL INSERT | Config |
| Lock Manager | lock_manager.py | 150 | PID 기반 동시성 제어 | Config |
| Main Orchestrator | worklog_reconcile.py | 200 | 전체 조율 | 모두 |
| Systemd Service | worklog-reconcile.service | 20 | 타이머 설정 | None |

### 프로토콜 사양 (MACHINE-READABLE-SPEC.md 기반)

#### Exit Codes
```
0 = Success (모든 커밋 기록됨)
1 = Transient Error (재시도 가능, systemd 자동 처리)
2 = Skip (Lock collision, 정상 스킵)
3 = Alert (DB 다운, 수동 개입 필요)
4 = Permanent Error (설정 오류, 해결 필수)
5 = Git Broken (저장소 손상, 긴급)
```

#### JSON 로깅
```json
{
  "timestamp": "2026-05-25T14:30:00Z",
  "level": "INFO",
  "component": "git_scanner",
  "function": "scan_recent_commits",
  "event_type": "scan_complete",
  "message": "Scanned 23 commits in last 15 minutes",
  "data": {
    "commit_count": 23,
    "time_window_minutes": 15,
    "duplicate_filtered": 2
  }
}
```

---

## ✅ PHASE 5: B-Plan 스테이징 테스트

### 유닛 테스트 (15분)
```bash
pytest tests/test_git_scanner.py -v
pytest tests/test_llm_caller.py -v
pytest tests/test_db_saver.py -v
pytest tests/test_lock_manager.py -v
```

### 통합 테스트 (20분)
```bash
# 스테이징 환경에서 완전한 파이프라인 실행
python3 scripts/worklog_reconcile.py --mode=staging --dry-run

# 실제 기록 테스트 (스테이징 DB 격리)
python3 scripts/worklog_reconcile.py --mode=staging
```

### 성능 테스트 (10분)
```bash
# 300개 커밋 × LLM 요약 처리
# 예상 시간: 300 × 5초(LLM) = 1500초 = 25분 (타이머 30분 윈도우 내)
# 타임아웃 없음 확인
```

---

## 🚀 PHASE 6: B-Plan 프로덕션 전환

### 컷오버 체크리스트
```yaml
pre_cutover:
  - backup_current_v1_1: "✅ git stash + DB snapshot"
  - staging_tests_passed: "✅ 모든 테스트 통과"
  - monitoring_metrics_collected: "✅ 2주 데이터 수집"
  - rollback_plan_documented: "✅ recovery.sh 작성"

cutover_steps:
  - 01_stop_v1_1_timer: "systemctl --user stop review-worker.timer"
  - 02_deploy_b_plan_code: "git pull + systemd 파일 배포"
  - 03_run_db_migration: "worklog_reconcile.py --init-schema"
  - 04_start_b_plan_timer: "systemctl --user enable/start worklog-reconcile.timer"
  - 05_monitor_first_run: "journalctl -f -u worklog-reconcile.service"
  - 06_verify_24h: "모니터링 24시간, 에러율 < 1%"

rollback_steps:
  - 01_stop_b_plan: "systemctl --user stop worklog-reconcile.timer"
  - 02_restore_v1_1: "git checkout <v1.1-commit>"
  - 03_restart_old_timer: "systemctl --user start review-worker.timer"
```

---

## 📍 현재 위치 (2026-05-18)

- ✅ B-Plan 설계 완료 (MACHINE-READABLE-SPEC.md, INDEX.md)
- ✅ 3가지 시스템 맹점 분석 완료
- ⏳ **다음**: PHASE 1 (v1.1 배포) 승인 & 실행

---

## 📎 참고 문서

- **전체 설계**: `/opt/projects/server/docs/worklog-b-plan-complete.tar.gz` (Azure Blob 링크)
- **기계 프로토콜**: `MACHINE-READABLE-SPEC.md`
- **실행 가이드**: `DETAILED-IMPLEMENTATION-PLAN.md`
- **마스터 인덱스**: `INDEX.md`

---

**결정**: v1.1 배포 → 2주 모니터링 → B-Plan 전환  
**승인 필요**: PHASE 1 시작 (session_guard.py 수정 + DB UNIQUE constraint)
