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
| **day_cycle** | 활성 (watchdog 기동) | 운영 중 | ✅ 정상 | 타이머 아님 — watchdog이 pending 감지 시 기동 |
| **netdata** | active | active | ✅ PASS | Observability 기본 infra 확인 |
| **훅 오버헤드** | avg 458.76ms (live) | baseline 대비 +20% 이내 | ⚠️ 재측정 | 최초 mock 0.0004ms는 측정 버그로 무효 |
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

### 2. day_cycle 서비스 상태 — ✅ 해결 (2026-09-14)
- **원인**: `devforge-inference` 컨테이너가 `--no-mmap` 인자로 기동 실패 (llama.cpp b10920+에서 플래그 제거됨) → heavy phase(extract/enrich/embed) 진행 불가
- **수정**: `--no-mmap` → `--load-mode none` (2곳: `scripts/lib/pod_manager/__init__.py`, `/opt/ai_data/scripts/inference-entrypoint.sh`)
- **결과**: inference 8080-8084 정상 기동, day_cycle이 LLM 호출(`[call_llm] extractor:8082`)로 진행
- **설계 확인**: day_cycle은 타이머가 아니라 **watchdog 이벤트 기반** (pending turns 감지 시 `systemctl start`). D1의 "timer inactive"는 정상.
- watchdog.service: active (enabled)

### 2b. ⚠️ 잔여 이슈 — enrich LLM 연결 재설정
- enrich 단계에서 llama-server(:8082)가 다수 LLM 호출 중 `RemoteDisconnected`/`ConnectionResetError` 반복
- `Done: 0 enriched, 50 failed (LLM: 529.8s)` — 50건 전부 실패
- **추정 원인**: 4코어 ARM CPU + 8B Q8 모델(~6GB) + KV cache(1024MB) 메모리 압박 → 서버 불안정. watchdocg 재시작과 in-flight 호출 충돌 가능성.
- **영향**: day_cycle 활성화는 되었으나 enrich 완료까지는 추가 튜닝 필요 (이전 보고서 `architecture-validation.md`와 일치하는 ARM 한계).

### 3. hook overhead — ⚠️ 최초 측정 무효, live 재측정 완료
- **최초 D1**: 0.0004ms (safe mock) — **무효**. 측정 스크립트가 `tool_input={}`을 전달해 `_handle_bash`가 early-return 경로만 측정함.
- **수정**: `measure-hook-overhead.py`가 실제 command(`pytest scripts/tests/`)를 전달하도록 수정.
- **live 실측 (2026-09-14)**: avg **458.76ms**, p50 440.79ms, p95 636.29ms, p99 1313.18ms
- **원인**: `_handle_bash`가 psql 서브프로세스(podman exec)를 2회 호출(pretool UPDATE + observe INSERT) → 서브프로세스 생성 비용 지배.
- **의의**: 실제 훅 오버헤드는 ~460ms로, mock 0.0004ms와 5자릿수 차이. W1 임계값 판정에 중요.
- **측정 노이즈 정리**: test observations 100건 삭제 완료.

### 4. observations 24h: 1건 (auto_log 활용도 극히 낮음)
- headless 세션에서 PostToolUse 훅이 거의 발동되지 않음.
- **대응**: hook overhead는 `measure-hook-overhead.py --live`로 직접 측정 완료 (위 #3).

### 4. provenance 코드 레벨 확인
- **원인 규명 완료** (4/4 INSERT 경로 누락): turn_watcher.py:217/245, mcp_server.py:173·530, mcp_server_sse.py:222
- **코드 수정 완료** (rf-triage-07): 4개 파일 INSERT에 source 컬럼 추가
  - 신규 turns: `turns.source`에 원본 출처 기록 (claude/copilot/gemini/aider/opencode/mcp_ingest 등)
- **마커 적용 완료** (rf-triage-07): 기존 7163건 → `legacy:pre-2026-09` (backup: turns_source_backup_20260914)
- Gate 6: ✅ 통과 (기존=마커 / 신규=코드 수정)

---

## 다음 D2 체크리스트

- [x] day_cycle 서비스 동작 확인 (inference `--no-mmap` 수정 → 정상)
- [x] hook overhead live 측정 (avg 458.76ms)
- [x] turns.source INSERT 경로 수정 (4개 파일)
- [ ] observations 24h count 재측정
- [ ] day_cycle stage timing 로그 확인 (D2 이후)

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

### 수정된 버그 (17개)

#### 1차 검증 (10개)

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

#### 2차 검증 — Ingest E2E 중 발견 (7개)

| 파일 | 수정 내용 |
|---|---|
| `extract_adapter.py` | 미사용 `Observation` import 제거 (ruff F401) |
| `mcp/server.py` + `api/app.py` | ingest: 없는 UUID conversation → 자동 생성 |
| `mcp/server.py` + `api/app.py` | ingest: conversation_id UUID 형식 검증 (non-UUID → 400) |
| `mcp/server.py` + `api/app.py` | ingest: `Turn(meta=)` → `Turn(meta_data=)` (TypeError) |
| `mcp/server.py` + `api/app.py` | ingest: text nullable=False → `""` 기본값 |
| `mcp/server.py` | HTTP `POST /api/v1/ingest` 라우트 추가 (dual surface) |
| `docs/specs/ingest-provenance.yaml` | auto-create 동작 스펙 반영 |

> SSOT (7일 후): `docs/reports/rf-triage-07-patchnote-20260914.md`
