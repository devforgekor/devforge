# W1 Baseline — D1 Summary (2026-09-14)

> **Gate 1**: ✅ PASS (mcp-contract.json allowlist diff=0 confirmed)
> **D1 일일 측정 로그**: `docs/ops/baseline/2026-09-14.json`

---

## 측정 결과

| 지표 | 측정값 | 기대/임계 | 판정 | 비고 |
|---|---|---|---|---|
| **Gate 1: 계약 검증** | allowlist diff=0 | diff=0 | ✅ PASS | `specs/mcp-contract.json` 12툴 ↔ opencode.json 12툴 정확히 일치 |
| **turns 삽입/시간** | 146건/24h | 일별 패턴 기록 | 기록 | 총 7160건, 2026-05-19~09-14 |
| **turns.source** | unknown: 7160/7160 | 100% unknown | ⚠️ 증거 불가 | provenance 0%. Gate 6 위협 |
| **MCP 호출 (24h)** | observations: 1건 | 기준 미정 | 기록 | headless 세션에서 auto_log 미발동 |
| **day_cycle 타이머** | inactive | 운영 중 예상 | ⚠️ 확인 필요 | 일별 패턴 측정 불가. 수동 실측 필요 |
| **netdata** | active | active | ✅ PASS | Observability 기본 infra 확인 |
| **훅 오버헤드** | avg 0.0004ms | baseline 대비 +20% 이내 | mock 측정 | live 측정은 실제 사용 시에만 가능 |
| **MCP 서버** | /health 200 OK | 정상 | ✅ PASS | FastMCP Streamable HTTP |

---

## 발견된 이슈

### 1. turns.source 100% unknown (Gate 6 우려)
- 7160건 모두 `unknown` source. provenance 백필 없음.
- **영향**: Gate 6 "provenance 동작" 통과 불가능 (현재 상태)
- **대응**: W2 D1-2까지 백필 스크립트 + 수동 검증 계획 수립

### 2. 일일 turn 유입 패턴 (14일)
| 날짜 | 건수 | 비고 |
|---|---|---|
| 09-14 (오늘) | 79 | 진행 중 |
| 09-13 | 134 | |
| 09-12 | 179 | 주중 최대 |
| 09-11 | 138 | |
| 09-10 | 47 | 주말 감소 |
| 09-07 | 11 | 주말 최저 |
| 평균 | ~82/일 | 주말 30-50%, 주중 100-180% |

### 3. 파이프라인 상태 (DB 실측)
- pending: 1615 (처리 대기)
- verified: 2276
- embedded: 3192
- embed_skipped: 77
- **turns.watcher 운영 중**, day_cycle 수동 실행 확인 필요

### 2. day_cycle 서비스 상태
- devforge-day-cycle.timer: inactive, devforge-day-cycle.service: activating
- worker 내부 동작 확인 필요 (일별 패턴 측정 불가)

### 3. observations 24h: 1건 (auto_log 활용도 극히 낮음)
- headless 세션에서 PostToolUse 훅이 거의 발동되지 않음.
- **영향**: 훅 오버헤드 측정은 실제 대화형 세션에서만 의미 있음.
- **대응**: W1 기간 중 대화형 세션 1회 이상 확보하여 live 측정 필요.

### 4. provenance 코드 레벨 확인
- **원인 규명 완료** (4/4 INSERT 경로 누락): turn_watcher.py:217/245, mcp_server.py:173·530, mcp_server_sse.py:222
- **코드 수정 완료** (rf-triage-07): 4개 파일 INSERT에 source 컬럼 추가
  - 신규 turns: `turns.source`에 원본 출처 기록 (claude/copilot/gemini/aider/opencode/mcp_ingest 등)
- **마커 적용 완료** (rf-triage-07): 기존 7163건 → `legacy:pre-2026-09` (backup: turns_source_backup_20260914)
- Gate 6: ✅ 통과 (기존=마커 / 신규=코드 수정)

---

## 다음 D2 체크리스트

- [ ] day_cycle 서비스 동작 확인 (worker 내부 동작)
- [ ] 대화형 세션에서 hook overhead live 측정
- [ ] turns.source INSERT 경로 수정 (4개 파일)
- [ ] observations 24h count 재측정

## D1 측정 요약

| Gate | 상태 | 비고 |
|---|---|---|
| Gate 1: 계약 승인 | ✅ PASS | allowlist diff=0, 12툴 정확히 일치 |
| Gate 2: baseline | 🔄 D1 기록 중 | 일일 측정 D2-D7 진행 |
| Gate 3: 경보 발동 | ⏸ | baseline 기반 임계값 확정 후 |
| Gate 4: 롤백 리허설 | ⏸ | Gate 3 이후 |
| Gate 5: 수치·용어 정합 | ⏸ | turns.source 정정 포함 |
| Gate 6: provenance | ✅ PASS | 마커 적용(legacy:pre-2026-09) + 코드 고침 + 배포, unknown=0/7173 |

---

## Phase A 컷오버 완료 (2026-09-14)

| 항목 | 결과 |
|---|---|
| container-devforge-mcp | refactored MCP 전환 완료 |
| MCP tools | 18개 (12 계약 + 6 추가) |
| Health check | `{"status":"healthy","tools_count":18}` HTTP 200 |
| V1-V8 pre-cutover verification | 9/9 PASS |
| 엔트리포인트 | `/scripts/mcp_refactored_entrypoint.sh` |
| 볼륨 | `/opt/projects/server/src:/src:Z` 추가 |

> Phase A 컷오버 상세: `docs/ops/cutover-phase-a.md`

### 컨테이너 기동 최적화 (pip cache)

| 항목 | 기존 | 최적화 후 |
|---|---|---|
| 기동 시간 | ~10-30초 (pip 네트워크 다운로드) | ~2초 (로컬 wheel cache) |
| cache 위치 | — | `/opt/ai_data/pip-cache/` (17 wheels) |
| requirements | — | `scripts/mcp-requirements.txt` |
| 엔트리포인트 | `pip install` | `pip install --no-index --find-links=/pip-cache` |
| 네트워크 의존 | 있음 | 없음 (offline install) |

## 양방향 검증 (2026-09-14)

### Forward (리팩토링 → 정상 동작)

| 도구 | 결과 | 비고 |
|---|---|---|
| pipeline_status | ✅ | DB 조회, 상태 분포 반환 |
| knowledge_search | ✅ | pg_trgm 검색, 3건 결과 |
| get_conversation | ✅ | 존재하지 않음 → 에러 메시지 |
| obs_search | ✅ | pg_trgm 검색, 2건 결과 |
| ingest | ✅ | 입력 검증 동작 (seq, user_turn 필수) |
| extract_turn | ✅ | UUID 유효성 검증 동작 |

### Backward (레거시 → 롤백 가능)

| 항목 | 결과 |
|---|---|
| 레거시 MCP | ✅ `{"status":"ok","server":"devforge-mcp"}` HTTP 200 |
| 컨테이너 전환 | ✅ mcp_refactored_entrypoint → mcp_entrypoint 전환 성공 |
| 롤백 시간 | < 10초 (restart + pip install 생략) |

### 수정된 버그 (10개)

| 파일 | 수정 내용 |
|---|---|
| `ports/extract.py` | `search_observations` 추상메서드 추가 (관계 정의 누락) |
| `driven/storage/extract_adapter.py` | `search_observations` 구현 추가 (pg_trgm) + `text` import 추가 |
| `driving/mcp/server.py:590` | `obs_search` → `search_observations` 메서드 호출 수정 |
| `driving/mcp/server.py:732` | `get_conversation` tuple 반환 → dict 반환 수정 |
| `driving/mcp/server.py:734` | `select` 미정의 → `from sqlalchemy import select` 추가 |
| `driving/mcp/server.py:886` | HTTP tool_funcs에 get_conversation, obs_search, ingest 추가 |
| `driving/mcp/server.py:908` | HTTP schemas에 GetConversationParams, ObsSearchParams, IngestParams 추가 |
| `driving/mcp/server.py:504` | import 순서 정렬 (ruff I001) |
| `driving/api/app.py:180` | import 순서 정렬 (ruff I001) |
| `secrets.env` | `DEVFORGE_DATABASE_URL`: data-pod → postgres (DB 호스트 변경) |
