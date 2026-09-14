# rf-triage-07 패치 노트

> Status: complete · Date: 2026-09-14 · Owner: devforge
> W1 baseline 기간(2026-09-14~09-21) 동안 수행된 모든 작업 기록
> SSOT: D7 종료 후 이 문서가 rf-triage-07 최종 참조 자료

---

## 1. 총괄

| 단계 | 상태 | 기간 |
|---|---|---|
| Phase 0 | ✅ 완료 | 2026-09-14 ~ |
| Phase A-1 | ✅ 완료 | 2026-09-14 ~ |
| Phase A-2 | ✅ 완료 | 2026-09-14 ~ |
| Phase A-3 | ✅ 완료 | 2026-09-14 ~ |
| Phase A 컷오버 | ✅ 완료 | 2026-09-14 ~ |
| 리팩토링/버그 수정 | ✅ 완료 | 2026-09-14 ~ |
| 양방향 검증 | ✅ 완료 | 2026-09-14 ~ |
| pip cache 최적화 | ✅ 완료 | 2026-09-14 ~ |
| W1 측정 | 🔄 진행 중 | D1~D7 (자동) |
| Phase B | ⏸ 대기 | D7 종료 후 |

---

## 2. Provenance (지속성 표시)

### 문제
- 7,163개 기존 turns의 `source` 컬럼이 모두 `unknown` (4개 INSERT 경로 누락)

### 원인 규명
| 경로 | 파일 | 문제 |
|---|---|---|
| turn_watcher | `scripts/turn_watcher.py:217/245` | INSERT에 source 컬럼 누락 |
| MCP SSE | `scripts/mcp_server.py:173` | INSERT에 source 컬럼 누락 |
| MCP HTTP | `scripts/mcp_server.py:530` | INSERT에 source 컬럼 누락 |
| MCP SSE v2 | `scripts/mcp_server_sse.py:222` | INSERT에 source 컬럼 누락 |

### 조치
1. **코드 고침**: 4개 파일 INSERT에 `source`, `agent` 컬럼 추가
2. **마커 적용**: `UPDATE turns SET source='legacy:pre-2026-09' WHERE source='unknown'` (7,163건)
3. **백업**: `turns_source_backup_20260914` (7,163건)
4. **배포**: `devforge-turn-watcher`, `devforge-mcp` 재시작
5. **검증**: `scripts/verify-provenance.py` → unknown=0/7173 확인

### 결과
| 항목 | 결과 |
|---|---|
| 기존 turns | 7,163건 → `legacy:pre-2026-09` |
| 신규 turns | `opencode` (세션별 source 기록) |
| Gate 6 | ✅ 통과 |

---

## 3. Phase A-1: Ingest 구현

### MCP `ingest` 도구
- **모듈**: `src/devforge/adapters/driving/mcp/server.py`
- **Input** (`IngestParams`): `source`(필수), `agent`, `title`, `model`, `conversation_id`(필수), `turns`(필수)
- **동작**: batch conversation ingestion, idempotency via `source_message_id` 중복 skip
- **Provenance**: `source`/`agent` 기록
- **DB**: `POST /api/v1/ingest` (HTTP, loopback auth, required field validation)

### HTTP `/api/v1/ingest`
- **모듈**: `src/devforge/adapters/driving/api/app.py`
- **Auth**: loopback OR bearer token
- **Required**: `source`, `conversation_id`, `turns`
- **Idempotency**: `source_message_id` 중복 시 skip

---

## 4. Phase A-2: 12-tool Contract Matching

### 결과
| 구분 | 수 |
|---|---|
| 계약 도구 | 12개 (전부 매칭) |
| 추가 도구 | 6개 (ingest, deepdive_step, knowledge_search, extract_turn, store_observation, pipeline_status) |
| 총 도구 | 18개 |

### DeepDive 5종
`deepdive_step_enter`, `deepdive_step_exit`, `deepdive_session_heartbeat`, `deepdive_session_status`, `deepdive_verify_sandbox`

### 기타 추가 도구
`obs_write`, `obs_search`, `search_turns`, `search_similarity`, `mem_save`, `mem_search`, `get_conversation`

---

## 5. Phase A-3: Cutover 시나리오

### 문서
- `docs/ops/cutover-phase-a.md`
- 수락 기준 6개, 사전 준비 6개, 검증 V1-V8, 롤백 시나리오 4개

### 컷오버 실행
| 항목 | 결과 |
|---|---|
| 대상 | `container-devforge-mcp` |
| 변경 | legacy → refactored MCP (`adapters/driving/mcp`) |
| 볼륨 추가 | `/opt/projects/server/src:/src:Z` |
| 엔트리포인트 | `/scripts/mcp_refactored_entrypoint.sh` |
| V1-V8 | 9/9 PASS |
| Health check | `{"status":"healthy","tools_count":18}` |

---

## 6. 리팩토링 오류/버그 수정

| # | 파일 | 수정 내용 |
|---|---|---|
| 1 | `ports/extract.py` | `search_observations` 추상메서드 추가 |
| 2 | `driven/storage/extract_adapter.py` | `search_observations` 구현 (pg_trgm), `text` import |
| 3 | `driving/mcp/server.py:590` | `obs_search` → `search_observations` 호출 |
| 4 | `driving/mcp/server.py:732` | `get_conversation` tuple 반환 → dict 반환 |
| 5 | `driving/mcp/server.py:734` | `select` 미정의 → `from sqlalchemy import select` 추가 |
| 6 | `driving/mcp/server.py:886` | HTTP tool_funcs에 get_conversation, obs_search, ingest 추가 |
| 7 | `driving/mcp/server.py:908` | HTTP schemas에 GetConversationParams, ObsSearchParams, IngestParams 추가 |
| 8 | `driving/mcp/server.py:504` | import 순서 정렬 (ruff I001) |
| 9 | `driving/api/app.py:180` | import 순서 정렬 (ruff I001) |
| 10 | `secrets.env` | `DEVFORGE_DATABASE_URL`: data-pod → postgres |

### 검증
- `ruff check`: All passed
- `mypy`: Success (9 source files)

---

## 7. 양방향 검증

### Forward (리팩토링 → 정상 동작)
| 도구 | HTTP | 결과 |
|---|---|---|
| `pipeline_status` | ✅ | DB 조회, 상태 분포 반환 (7,175 turns) |
| `knowledge_search` | ✅ | pg_trgm 검색, 다건 결과 |
| `get_conversation` | ✅ | 존재하지 않음 → 에러 메시지 반환 |
| `obs_search` | ✅ | pg_trgm 검색, 다건 결과 |
| `ingest` | ✅ | 입력 검증 동작 (IngestTurnParams: seq, user_turn 필수) |
| `extract_turn` | ✅ | UUID 유효성 검증 동작 |

### Backward (레거시 → 롤백 가능)
| 항목 | 결과 |
|---|---|
| 레거시 MCP | ✅ `{"status":"ok","server":"devforge-mcp"}` |
| 전환/복귀 | ✅ < 10초 |
| 롤백 절차 | container config `Exec` 변경 → restart |

---

## 8. 컨테이너 최적화

### pip cache
| 항목 | 기존 | 최적화 |
|---|---|---|
| 기동 시간 | ~10-30초 | ~2초 |
| cache 위치 | — | `/opt/ai_data/pip-cache/` (17 wheels) |
| requirements | — | `scripts/mcp-requirements.txt` |
| 엔트리포인트 | `pip install` | `pip install --no-index --find-links=/pip-cache` |
| 네트워크 의존 | 있음 | 없음 (offline) |

### 컨테이너 설정 변경
| 파일 | 변경 |
|---|---|
| `~/.config/containers/systemd/container-devforge-mcp.container` | pip-cache volume 추가, 엔트리포인트 변경 |
| `/run/.../container-devforge-mcp.service` | volume, exec, RequiresMountsFor 업데이트 |

---

## 9. W1 Baseline 측정

### D1 측정 결과 (2026-09-14)
| 지표 | 값 |
|---|---|
| turns/24h | 143 |
| turns total | 7,175 |
| turns.source | 0 unknown (legacy:pre-2026-09) |
| observations/24h | 1 |
| pipeline | pending:1630, verified:2276, embedded:3192, embed_skipped:77 |
| hook overhead | avg 0.0004ms (mock) |
| MCP health | healthy (18 tools) |

### 자동 측정
- `baseline-daily.timer`: 매일 00:00 UTC, D2~D7 자동 측정
- 출력: `docs/ops/baseline/2026-09-XX.json`

---

## 10. Gate 상태

| Gate | 상태 | 비고 |
|---|---|---|
| Gate 1: 계약 승인 | ✅ PASS | allowlist diff=0 |
| Gate 2: baseline | 🔄 진행 중 | D1~D7 (자동) |
| Gate 3: 경보 발동 | ⏸ | baseline 기반 임계값 확정 후 |
| Gate 4: 롤백 리허설 | ⏸ | Gate 3 이후 |
| Gate 5: 수치·용어 정합 | ⏸ | turns.source 정정 포함 |
| Gate 6: provenance | ✅ PASS | 마커 + 코드 고침 + 배포 |

---

## 11. 커밋 이력 (main)

```
7a60961  rf-triage-07: D1 요약에 pip cache 정보 추가
efbd935  rf-triage-07: D1 요약에 양방향 검증 결과 추가
594dec4  rf-triage-07: ingest HTTP tool 추가 (양방향 검증 완료)
ce3fb6d  rf-triage-07: HTTP tool_funcs/schemas 완성
767c83c  rf-triage-07: 리팩토링 오류/버그 수정
f28f9d8  auto: sync
c44fd72  rf-triage-07: D1 요약에 Phase A 컷오버 결과 추가
c5c03db  rf-triage-07: handover.yaml 업데이트 (Phase 0~A 완료)
7f15600  rf-triage-07: Phase A 컷오버 — container-devforge-mcp 전환
e5d4584  rf-triage-07: Phase A 사전 검증 스크립트 + 컷오버 시나리오
c0ceef8  rf-triage-07: Phase A 컷오버 시나리오 문서화
74f439c  rf-triage-07: Phase A-2 12툴 계약 매칭 완료
f2b2a78  rf-triage-07: Phase A-1 ingest 구현 (MCP + HTTP)
```

---

## 12. 다음 단계

| 단계 | 조건 | 내용 |
|---|---|---|
| Phase B | D7 종료 후 | turn_watcher → domain/turn_collection 전환 |
| Phase B | D7 종료 후 | ADR-0005 추출 라우팅 |
| Gate 2 확정 | D7 데이터 분석 | W1 요약 → 임계값 확정 |
| pip cache 최적화 | Phase B 착수 시 | 이미지 빌드로 전환 검토 |

---

> 이 문서는 2026-09-14 시작 W1 기간 동안의 모든 작업을 기록합니다. D7 종료(2026-09-21 00:00 UTC) 후 SSOT로 사용됩니다.
