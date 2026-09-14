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
| 신규 turns | `opencode` (세션별 source 기록, 본 문서 작성 시점 17건) |
| 검증 시점 unknown | 0/7,173 |
| 현재 unknown | 0/7,180 |
| Gate 6 | ✅ 통과 |

---

## 3. Phase A-1: Ingest 구현

### MCP `ingest` 도구
- **모듈**: `src/devforge/adapters/driving/mcp/server.py`
- **Input** (`IngestParams`): `source`(필수), `agent`, `title`, `model`, `conversation_id`(필수), `turns`(필수)
- **동작**: batch conversation ingestion, idempotency via `source_message_id` 중복 skip
- **Provenance**: `source`/`agent` 기록

### HTTP `POST /api/v1/ingest`
- **모듈**: `src/devforge/adapters/driving/api/app.py`
- **Auth**: loopback OR bearer token
- **Required**: `source`, `conversation_id`, `turns`
- **Idempotency**: `source_message_id` 중복 시 skip

---

## 4. Phase A-2: 12-tool Contract Matching

### 결과
| 구분 | 수 | 도구 |
|---|---|---|
| 계약 도구 | 12개 (전부 매칭) | deepdive_* 5종 + obs_* 2종 + search_* 2종 + mem_* 2종 + get_conversation |
| 추가 도구 | 6개 | ingest, deepdive_step, knowledge_search, extract_turn, store_observation, pipeline_status |
| 총 도구 | 18개 | (서버 `tools_count:18` 확인) |

### 계약 도구 12종 (mcp-contract.json)
- **DeepDive 5종**: `deepdive_step_enter`, `deepdive_step_exit`, `deepdive_session_heartbeat`, `deepdive_session_status`, `deepdive_verify_sandbox`
- **Observation 2종**: `obs_write`, `obs_search`
- **Search 2종**: `search_turns`, `search_similarity`
- **Memory 2종**: `mem_save`, `mem_search`
- **기타 1종**: `get_conversation`

### 추가 도구 6종 (계약 외)
`ingest`, `deepdive_step`(aggregate wrapper), `knowledge_search`, `extract_turn`, `store_observation`, `pipeline_status`

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

> 라인 번호는 **수정 전** 파일 기준. (ruff import 정렬 후 라인 이동 발생)

| # | 파일 | 수정 내용 |
|---|---|---|
| 1 | `ports/extract.py` | `search_observations` 추상메서드 추가 |
| 2 | `driven/storage/extract_adapter.py` | `search_observations` 구현 (pg_trgm), `text` import |
| 3 | `driving/mcp/server.py:590` | `obs_search` → `search_observations` 호출 (현재:592) |
| 4 | `driving/mcp/server.py:732` | `get_conversation` tuple 반환 → dict 반환 (현재:730) |
| 5 | `driving/mcp/server.py:734` | `select` 미정의 → `from sqlalchemy import select` 추가 (현재:727) |
| 6 | `driving/mcp/server.py:886` | HTTP tool_funcs에 get_conversation, obs_search, ingest 추가 (현재:884) |
| 7 | `driving/mcp/server.py:908` | HTTP schemas에 GetConversationParams, ObsSearchParams, IngestParams 추가 (현재:902) |
| 8 | `driving/mcp/server.py:504` | import 순서 정렬 (ruff I001) |
| 9 | `driving/api/app.py:180` | import 순서 정렬 (ruff I001) |
| 10 | `secrets.env` | `DEVFORGE_DATABASE_URL`: data-pod → postgres |

### 2차 검증에서 추가 수정 (2026-09-14, Forward E2E 중 발견)

| # | 파일 | 수정 내용 |
|---|---|---|
| 11 | `driven/storage/extract_adapter.py:313` | 미사용 `Observation` import 제거 (ruff F401) |
| 12 | `driving/mcp/server.py` / `api/app.py` | ingest: 없는 UUID conversation → **자동 생성** (기존 404/에러) |
| 13 | `driving/mcp/server.py` / `api/app.py` | ingest: `conversation_id` UUID 형식 검증 (non-UUID → 명시적 400) |
| 14 | `driving/mcp/server.py` / `api/app.py` | ingest: `Turn(meta=)` → `Turn(meta_data=)` (속성명 불일치 TypeError) |
| 15 | `driving/mcp/server.py` / `api/app.py` | ingest: `text` nullable=False인데 None 전달 → `""` 기본값 |
| 16 | `driving/mcp/server.py` | HTTP `POST /api/v1/ingest` 라우트 추가 (스펙: HTTP+MCP dual surface) |
| 17 | `docs/specs/ingest-provenance.yaml` | conversation_id auto-create 동작 스펙 반영 |

### 검증
- `ruff check`: All passed
- `mypy`: Success (38 source files)
- V1-V8 pre-cutover: 9/9 PASS

---

## 7. 양방향 검증

### Forward (리팩토링 → 정상 동작)
| 도구 | 서피스 | 결과 |
|---|---|---|
| `pipeline_status` | HTTP `/tools/` | ✅ DB 조회, 상태 분포 반환 |
| `knowledge_search` | HTTP `/tools/` | ✅ pg_trgm 검색, 다건 결과 |
| `get_conversation` | HTTP `/tools/` | ✅ 존재하지 않음 → 에러 메시지 반환 |
| `obs_search` | HTTP `/tools/` | ✅ pg_trgm 검색, 다건 결과 |
| `ingest` | MCP `/tools/ingest` + **HTTP `/api/v1/ingest`** | ✅ 입력 검증 (seq, user_turn 필수) |
| `extract_turn` | HTTP `/tools/` | ✅ UUID 유효성 검증 동작 |

### Ingest E2E (2차 검증)
| 시나리오 | 결과 |
|---|---|
| 신규 UUID conversation auto-create | ✅ 2건 inserted |
| 비어있는 conversation_id (uuid4 자동 생성) | ✅ 1건 inserted |
| idempotency (동일 `source_message_id` 재전송) | ✅ 1 inserted / 1 skipped |
| non-UUID conversation_id | ✅ 명시적 에러 ("must be UUID") |
| HTTP `/api/v1/ingest` (port 8000) | ✅ 1 inserted, DB 반영 확인 |

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

### D1 측정 결과 (2026-09-14, 스냅샷)
| 지표 | 값 |
|---|---|
| turns/24h | 143 |
| turns total | 7,163 (마커 시점) → 7,175 (D1) → **7,180 (본 문서 작성 시점)** |
| turns.source | 0 unknown — legacy:pre-2026-09 7,163 + opencode 17 |
| observations/24h | 1 |
| pipeline | pending:1630, verified:2276, embedded:3192, embed_skipped:77 |
| hook overhead | avg **458.76ms** (live) — 최초 mock 0.0004ms는 측정 버그로 무효 |
| MCP health | healthy (18 tools) |

> turns 수치는 매일 증가 (W1 측정 기간 중 계속 유입)

### 자동 측정
- `baseline-daily.timer`: 매일 00:00 UTC, D2~D7 자동 측정
- 출력: `docs/ops/baseline/2026-09-XX.json`

---

## 9b. day_cycle 활성화 + hook 재측정 (2026-09-14)

### day_cycle 활성화

| 항목 | 내용 |
|---|---|
| 근본 원인 | `devforge-inference`가 `--no-mmap` 인자로 기동 실패 (llama.cpp b10920+에서 플래그 제거) |
| 수정 | `--no-mmap` → `--load-mode none` (2곳: `scripts/lib/pod_manager/__init__.py`, `/opt/ai_data/scripts/inference-entrypoint.sh`) |
| 결과 | inference 8080-8084 정상 기동, day_cycle LLM 호출 진행 |
| 설계 | day_cycle은 타이머 아님 — **watchdog 이벤트 기반** (pending 감지 시 기동) |
| 잔여 | enrich에서 llama-server 연결 재설정 반복 (ARM CPU 메모리 압박) |

### hook overhead 재측정

| 항목 | 내용 |
|---|---|
| 최초 (무효) | avg 0.0004ms (safe mock) — 측정 스크립트가 `tool_input={}` 전달 → early-return만 측정 |
| 수정 | `measure-hook-overhead.py`가 실제 command(`pytest scripts/tests/`) 전달 |
| live 실측 | avg **458.76ms**, p50 440.79ms, p95 636.29ms, p99 1313.18ms |
| 원인 | `_handle_bash`가 psql 서브프로세스 2회 호출 (pretool UPDATE + observe INSERT) |
| 정리 | test observations 100건 삭제 |

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

## 11. 커밋 이력 (main, 전체)

```
d3b1569  rf-triage-07: ingest 버그 수정 + HTTP surface 노출 (양방향 검증)
eb6cbde  rf-triage-07: 패치 노트 + 핸드오버 검증·정정
2852a6e  docs: D1 요약에 SSOT 참조 추가
9b9946e  docs: INDEX.md에 rf-triage-07 패치 노트 추가
856ef21  rf-triage-07: 패치 노트 문서 추가 (SSOT용)
7a60961  rf-triage-07: D1 요약에 pip cache 정보 추가
d10dea9  rf-triage-07: pip cache for container startup
efbd935  rf-triage-07: D1 요약에 양방향 검증 결과 추가
594dec4  rf-triage-07: ingest HTTP tool 추가 (양방향 검증 완료)
ce3fb6d  rf-triage-07: HTTP tool_funcs/schemas 완성
767c83c  rf-triage-07: 리팩토링 오류/버그 수정
f28f9d8  auto: sync 2026-09-14
c44fd72  rf-triage-07: D1 요약에 Phase A 컷오버 결과 추가
c5c03db  rf-triage-07: handover.yaml 업데이트 (Phase 0~A 완료)
7f15600  rf-triage-07: Phase A 컷오버 — container-devforge-mcp 전환
e5d4584  rf-triage-07: Phase A 사전 검증 스크립트 + 컷오버 시나리오
c0ceef8  rf-triage-07: Phase A 컷오버 시나리오 문서화
74f439c  rf-triage-07: Phase A-2 12툴 계약 매칭 완료
f2b2a78  rf-triage-07: Phase A-1 ingest 구현 (MCP + HTTP)
64937fe  auto: sync 2026-09-14
42004cb  rf-triage-07: handover.yaml 업데이트 + provenance 검증 스크립트
865b4fe  rf-triage-07: W1 daily baseline timer documentation
42d04a7  rf-triage-07: D1 baseline 측정 데이터 최종
5e9eb7f  rf-triage-07: provenance 마커 적용 완료 (7163건 legacy:pre-2026-09)
9c2337f  rf-triage-07: provenance 마커 정책 결정 (옵션 A: legacy 마커)
5db8c56  rf-triage-07: provenance 코드 고침 — turns.source INSERT 수정 (4개 파일)
d9f6fe7  rf-triage-07: D1 수동 검증 — provenance 코드 레벨 확인
```

> rf-triage-07 전체 커밋 26개 (2026-09-14)

---

## 12. 다음 단계

| 단계 | 조건 | 내용 |
|---|---|---|
| Phase B | D7 종료 후 | turn_watcher → domain/turn_collection 전환 |
| Phase B | D7 종료 후 | ADR-0005 추출 라우팅 |
| Gate 2 확정 | D7 데이터 분석 | W1 요약 → 임계값 확정 |
| pip cache 최적화 | Phase B 착수 시 | 이미지 빌드로 전환 검토 |

---

## 13. Gudokpin API → Claude Code 연동 (2026-09-14)

### 목적
- `GUDOKPIN_API`(secrets.env)를 Claude Code 백엔드로 연결
- Gudokpin = GPT & Claude 통합 게이트웨이 (OpenAI 호환)

### 구현

| 항목 | 내용 |
|---|---|
| 프록시 | `scripts/proxies/anthropic_gudokpin.py` (Anthropic→OpenAI 변환, :44779) |
| 시스템드 | `anthropic-gudokpin-proxy.service` (enabled, running) |
| 업스트림 | `https://api.gudokpin.com/v1` |
| 키 | `GUDOKPIN_API` (secrets.env) |
| 모드 | `claude-mode set gudokpin` → `ANTHROPIC_BASE_URL=http://127.0.0.1:44779` |

### MODEL_MAP (Claude → Gudokpin)
| Claude 모델 | Gudokpin 모델 |
|---|---|
| claude | DeepSeek-V4-Flash-0731 |
| claude-pro | deepseek-v4-pro-0813 |
| claude-sonnet-4-* | claude-sonnet-5 |
| claude-opus-4-8 | claude-opus-5 |
| claude-haiku / claude-fable | claude-fable-5 |

### 검증
- `claude -p "Reply with exactly: GUDOKPIN_OK"` → **GUDOKPIN_OK** 응답
- Anthropic `/v1/messages` non-stream/stream 변환 동작
- 모드 파일: `MODE=gudokpin` (기본 백엔드)

> 참고: 스트리밍 중 Broken pipe는 클라이언트 조기 종료 시 정상.

---

> 이 문서는 2026-09-14 시작 W1 기간 동안의 모든 작업을 기록합니다. D7 종료(2026-09-21 00:00 UTC) 후 SSOT로 사용됩니다.
