# Peer Review Synthesis — activity-log-unified-plan v1.1

**Date**: 2026-05-22
**Reviewers**: 4 AI agents
**Outcome**: APPROVED with revisions incorporated

---

## 1. Accepted Changes (17 items)

### Schema

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 1 | `parent_id BIGINT` 자기참조 FK 추가 | R2, R3 | 기계 간 DAG 인과관계 추적. 3단계 릴레이에서 Phi-4→14B→32B 상속 체인을 DB에서 직접 쿼리 가능 |
| 2 | `trace_id TEXT` 추가 | R3 | 실행 체인 포렌식 추적용 UUID. 여러 run_id가 교차해도 단일 trace로 연결 |
| 3 | `queue_status TEXT DEFAULT 'unprocessed'` 추가 | R3 | 비동기 큐 상태 통제. 각 스테이지가 `unprocessed`→`consumed`로 마킹 |
| 4 | `exec_status TEXT DEFAULT 'DONE'` 추가 | R3 | INIT/RUN/DONE/FAIL 실행 상태. summary_status와 관심사 분리 |
| 5 | `summary_status` / `queue_status` / `exec_status` 3개 status 필드로 분리 | R3 | 기존 v1.0은 `status` 하나로 요약+실행+큐 상태를 모두 표현하려 함 — 관심사 혼합 |
| 6 | `idx_activity_stage_unique` 추가: UNIQUE(run_id, type, parent_id) | R4 | 파이프라인 중복 INSERT 방지 |
| 7 | `idx_activity_body_gin` 추가: GIN(body) | R4 | body JSONB 내 메트릭 검색 |
| 8 | `idx_activity_queue` 추가: (queue_status, created_at) | 자체 추가 | 비동기 큐 폴링 쿼리 최적화 |
| 9 | `idx_activity_trace`, `idx_activity_parent` 추가 | 자체 추가 | 포렌식 추적 쿼리 최적화 |

### Logic / Data flow

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 10 | 요약기 SELECT에 `body` 필드 포함 | R3 | **치명적 데이터 기아 버그 수정.** v1.0은 title+summary만 SELECT하고 body 제외 → 스테이지 결과 알맹이를 LLM이 못 봄 |
| 11 | `_insert_activity_stage()`가 `summary`에 유의미한 텍스트 기록 | R3 | v1.0은 summary='' → title만으로 요약 불가. 단계별 설명 자동 생성 |
| 12 | `created_at < NOW() - INTERVAL '5 minutes'` 가드 추가 | R1 | summarizer가 실행 중인 작업의 미완료 raw 이벤트를 읽는 race condition 방지 |
| 13 | JSON 파싱 실패 시 `summary_status='parse_failed'` + exit 0 | R1 | v1.0은 exit 1 → systemd 재시도 → 동일 실패 무한 루프. 실패 마킹 후 다음 주기에 짧은 프롬프트로 재시도 |

### Timer

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 14 | 타이머: KST 18:01 → **KST 07:30** | R2 | 야간 3단계 릴레이(23:00~02:00경) 완료 후, 관리자 출근 전 아침 브리핑으로 가치 |

### Documentation

| # | Change | Source | Reason |
|---|--------|--------|--------|
| 15 | Query Layer에 Slack Operator 연동 명시 | R2 | CLI만 언급했던 v1.0에서 `/devforge result` → activity_log 조회 → Block Kit 메시지 흐름 추가 |
| 16 | Section 2.3 Event flow: 동기식 → 비동기 3단계 릴레이로 전면 재작성 | R3 | v1.0은 구형 review_worker.py(단일 프로세스 내 extract→verify) 기준. 실제 아키텍처에 맞게 분산형 큐 소비 구조로 변경 |
| 17 | Section 4.4: review_worker.py가 `run_id`, `trace_id`, `queue_status='unprocessed'` 기록 | R3 | v1.0은 run_id 없이 단일 로그만 남김 → 14B가 소비 불가 |

---

## 2. Accepted — Q1~Q5 Peer Consensus

| Question | Decision | For | Against | Rationale |
|----------|----------|-----|---------|-----------|
| Q1: 14B vs 32B | **Qwen14B** | 4 | 0 | 항시 구동 + 요약은 정보압축(창의적 생성 아님) + 32B swap window 의존성 제거 |
| Q2: 그룹화 기준 | **LLM 위임 + 날짜 경계 규칙** | 4 | 0 | run_id 우선 그룹화, 동일 날짜 내에서 LLM이 판단. 자정 걸친 작업 분할 방지 |
| Q3: observations 흡수 | **분리 유지** | 4 | 0 | 시계열 메트릭 vs 이벤트 로그 — 데이터 형태 상이 |
| Q4: review_facts 기록 | **Run-level 요약만** | 4 | 0 | 개별 fact는 review_facts에, activity_log는 집계만 |
| Q5: Exit codes | **0/1 only** | 4 | 0 | systemd 재시도 + journald 에러 로깅으로 충분 |

---

## 3. Rejected Changes (3 items)

| # | Suggestion | Source | Reason for rejection |
|---|------------|--------|---------------------|
| R1 | `_sanitize_for_log()` PII 마스킹 함수 | R4 | **불필요한 조기 최적화.** activity_log는 내부망 PostgreSQL에 저장되며, body JSONB는 이미 시스템 내부 데이터(코드 diff, 토큰 카운트). API 키/비밀번호를 body에 넣는 코드 경로가 없음. 필요 시 추후 추가 |
| R2 | 요약기 32B fallback 옵션 | R1 | **아키텍처 단순화 우선.** Podman B :8081(phi-4-mini/phi-4) 단독으로 충분. fallback 로직은 복잡성만 증가. 32B는 MODE=code 전용으로, MODE=batch/normal 시간대에는 존재하지 않음 |
| R3 | Section 9 모니터링 쿼리를 activity_summarizer.py에 내장 | R4 | **관심사 분리.** 모니터링은 motd-gen.timer / gen_server_state.py가 담당. summarizer는 순수 요약+INSERT만 수행 |

---

## 4. Noted — Implementation Guidance (3 items)

구현 시 참고할 사항. 계획서 수정 불필요.

| # | Note | Source |
|---|------|--------|
| N1 | `git_commit_hash` UNIQUE 제약은 `type='commit'`에만 적용 — stage/review는 커밋 해시와 무관하게 run_id로 연결 | R1 |
| N2 | Phase A 배포 후 `raw → summarized` 전환률 1주일 모니터링 후 cutover | R4 |
| N3 | 활동 로그가 쌓이면 90일 이상 된 `summary_status='summarized'` 행은 body trimming 또는 파티셔닝 검토 | R4 |

---

## 5. Version History

| Version | Date | Status |
|---------|------|--------|
| v1.0 | 2026-05-22 | Initial proposal |
| v1.1 | 2026-05-22 | 17 accepted changes from 4 peer reviews incorporated |
| v1.1a | 2026-05-22 | Infrastructure correction: LiteLLM references removed (was deleted 2026-05-19). Summarizer endpoint corrected: Podman B :8081 (phi-4-mini/phi-4), not Podman A :8080 (Qwen3-4B is intermittently stopped). Actual container modes documented (normal/batch/code). |

---

*Report generated for human decision-maker review. Implementation ready per v1.1.*
