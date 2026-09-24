# DevForge 최종 계획서 — 컷오버 + MCP 최적화 + 의사결정 통합

> Status: active · Date: 2026-09-23 (초판 2026-09-14) · Owner: devforge
> Related: `docs/REFACTORING_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/MIGRATION_GUIDE.md`, `docs/adr/0005-extraction-routing.md`, `docs/adr/0006-mcp-tool-surface.md`, `plans/watchdog-standard-compliance.md`, `plans/2026-standard-gap-remediation.md`
> 이 문서가 **정본(단일 계획서)**이다. 아래 §1~§7은 이전에 분리돼 있던 컷오버 계획·MCP 툴 최적화·의사결정을 통합한다.
> 근거 원문: `docs/reports/industry-standard-comparison-20260914.md`, `docs/reports/mcp-tool-audit-20260914.md`.

---

## 0. 사용법
- **§7 통합 의사결정(D1~D9)** 의 `결정:` 칸에 답하면(예: `D1 승인, D7 B`) 실행 우선순위가 확정된다.
- 각 실행 단계는 **구현 → 수락기준 → 전환 → 롤백** 순서이며, "구현/수락" 통과 전에는 전환하지 않는다.

## 1. 현황 (As-Is)

### 1.1 리팩토링 완료분 (유지)
- `src/devforge`: `core/{config,logging,paths,exceptions}`, `domain/models`(live 정합), `ports/{extract,container,health_check,heartbeat,incident_repository,notification,recovery,state_persistence,types}`, `adapters/driven/{llm,storage,health,container,recovery,notification,research,proxy_utils}`, `adapters/driving/{api,mcp,cli_cmds}`, `application/{extract_pipeline,orchestrator,watchdog_service}`, `domain/watchdog/{monitoring,orchestration,recovery}`, `pipeline_stages/extract`.
- 인프라: Alembic baseline(`alembic check` clean), ORM↔live 정합, `docs/specs/schema.sql`(16 테이블), shadow DB(`devforge_shadow` 라이브).

### 1.2 컷오버 미완 (라이브 의존성)
- 라이브는 여전히 **systemd 유닛 30개 + Quadlet 컨테이너 다수**가 `scripts/*`를 실행.
- Phase 0/1/1.5 완료, **Phase 2(watchdog) shadow-run(2.5)** — `devforge-watchdog-v2.service`가 legacy와 병행. `IssueCollector`·MCP `watchdog_*` 분리 잔여.
- 은퇴: `gen_architecture.py`(문서생성기), Gemini 에이전트 세션 → `_archive/`.

### 1.3 MCP 툴 감사 (30일, opencode `part` DB)
- 활성 MCP **서버 4개**(devforge-mcp·yggdrasil·lsp·opencode-db), 서버 노출 툴 devforge 25·lsp 65.
- opencode `tools` allowlist가 **로드 33툴**로 프루닝(서버별 10~20 충족).
- **30일 0회 = 9개**: `lsp_proxy_artifact_{get,info,list}`,`lsp_find_symbol`,`lsp_inspect_symbol`,`lsp_get_symbol_source`,`lsp_detect_lsp_servers`,`yggdrasil_list_plans`.
- 총 로드툴 호출 475회. 빌트인(bash 10,697/read 3,202)이 MCP보다 압도적 → LSP read 계열이 빌트인으로 대체됨.

## 2. 목표 & 원칙
1. **툴은 "LLM이 판단해야 하는 것"만.** 결정론적·항상-실행·이벤트시점이 정해진 것은 **훅/로직/스크립트**로.
2. **1주제=정본 1개**(SSOT), **비파괴 전환**(구 자산 보존), **컴포넌트 1개씩**.
3. 표준 정합: 서버당 5~15(최대 10~20) 툴, 근접중복 merge, 0회 제거, 라이프사이클/plumbing 노출 최소화, `readOnly/destructive` annotation.
4. 추출은 **deterministic→소형로컬→클라우드 escalation**, 후보정/비동기(ADR-0005).

## 3. 업계 표준 대조 (요약)
| 계층 | 표준 | 현재 | 판정 |
|---|---|---|---|
| Capture | hook(결정론적) + 확장/relay(웹) | 로컬 파서 폴링, 웹-LLM CLI 운영(Qwen/DeepSeek) | Phase B(ingest) 연결 필요 |
| Ingestion | hook→queue→importer | turn_watcher→raw→worker | 부분 |
| Storage/Retrieval | FTS+vector+RRF, L0~L3 | Postgres+pgvector+FTS5+RRF | 일치(계층 일부) |
| Extraction | deterministic→LLM, 구조화, evidence, hybrid | 로컬 8B 배치 | 이탈(ADR-0005) |
| Serving(MCP) | 5~15 툴/서버, 근접중복 merge | 로드 33(allowlist), 0회 9 | 개선 필요 |

출처: `reports/industry-standard-comparison-20260914.md` (AWS/Gingerlabs/Albato·Speakeasy/Copilot 40→13/RAG-MCP/NSA·OWASP).

> **웹-LLM 컴포넌트 (2026-09-20)**: `chrome-web-llm` CLI가 운영 중이다 — 로그인된 웹 LLM(Qwen/DeepSeek)을
> Playwright Chromium + `chrome-cli-bridge` 확장 + relay(:9876)로 구동, 세션 저장(수집)과 모델 간 핸드오프(공유) 지원.
> 운영: `runbooks/web-llm.md` / 상세: `~/.local/share/chrome-web-llm/README.md`. 파이프라인 수집 연결은 Phase B.

## 4. MCP 툴 최적화 계획

### 4.1 판별 기준 (훅 / 로직 / 툴)
| 대상 | 훅(이벤트 자동) | 로직/스크립트 | MCP 툴 |
|---|---|---|---|
| 결정론적·항상 실행·시점 고정 | ● | | |
| 판단 필요(검색/계획/쓰기) | | | ● |
| 대량/배치/운영 | | ● | |
| 툴 1개 + 내부 결정론 | | ●(파사드 내부) | ●(파사드) |

### 4.2 검토안 (Options)
- **A. 최소 컷(allowlist만)**: 0회 9툴을 `tools:false`. 변경=config. 저위험·저효과. **33→24**.
- **B. 훅/로직 전환 + Merge (권장)**: deepdive lifecycle·obs 자동캡처를 훅/플러그인화, 근접중복 merge. **33→약 12**. 중간 노력, 중간 리스크.
- **C. 점진공개/게이트웨이**: `search_tools`/`defer_loading`. allowlist 없는 대규모 클라이언트/툴 100+ 대비. 현재엔 과함(조건부).
- **D. 서버 파사드 전면 재편**: 네임스페이스+`action` 통일 재설계. 컷오버(ADR-0006 12툴 계약)와 **함께** 진행해야 정합. 대규모.
- **E. 무변경**: 기준선. 표준 미달 지속.

**비교 매트릭스**
| Option | 툴 수 | 노력 | 리스크 | 표준부합 | 롤백성 |
|---|---|---|---|---|---|
| A | 24 | 低 | 低 | 中 | 즉시(config) |
| **B** | **~12** | 中 | 中 | **高** | config+훅 되돌림 |
| C | 33(동일) | 高 | 中 | 高(대규모) | 복잡 |
| D | ~12 | 高 | 高 | 高 | 어려움 |
| E | 33 | 0 | — | 低 | — |

**권장: A 즉시 + B 단계적.** C/D는 조건 충족 시(무필터 클라이언트 다수 / 컷오버 시) 적용.

### 4.3 툴별 매핑 (33 → 목표 ~12)
| 서버 | 툴 | 30d | 조치 | 담당 |
|---|---|---:|---|---|
| devforge | `deepdive_step_enter/exit/session_heartbeat/session_status` | 60/58/11/1 | **훅/로직**(+서버 1툴 `deepdive(action)` 옵션) | 클라이언트 훅/플러그인 |
| devforge | `obs_write` | 67 | **PostToolUse 자동캡처**(수동툴 0~1) | 훅 |
| devforge | `mem_save/mem_search` + `obs_search` | 13/11/8 | **Merge `memory(action,kind)`** | MCP(파사드) |
| devforge | `search_turns` + `search_similarity` | 10/8 | **Merge `search_turns(mode)`** | MCP(파사드) |
| devforge | `get_conversation` | 15 | Keep | MCP |
| devforge | `deepdive_verify_sandbox` | 1 | Keep(+`destructiveHint`) | MCP |
| lsp | `get_diagnostics`,`blast_radius`,`find_references` | 25/23/4 | Keep | MCP |
| lsp | `start_lsp`,`detect_lsp_servers` | 13/2 | **자동/제거** | 클라이언트 |
| lsp | `find_symbol`,`inspect_symbol`,`get_symbol_source`,`suggest_fixes`,`rename_symbol` | 0 | **Remove**(후보) | — |
| lsp | `proxy_artifact_get/info/list` | 0 | **Remove** | — |
| yggdrasil | `deep_planning` | 112 | Keep | MCP |
| yggdrasil | `sequential_thinking` | 14 | **Merge(plan)** 검토 | MCP |
| yggdrasil | `get_plan`,`list_plans` | 1/0 | Keep / **Remove** | MCP |
| opencode-db | `query` / `list_tables`+`schema` | 5/10/3 | Keep / **Merge 1** | MCP |

### 4.4 단계 (Phase M0~M4)
| 단계 | 작업 | 산출물 | 수락기준 | 롤백 |
|---|---|---|---|---|
| **M0** | 0회 툴 allowlist off | `opencode.json` diff | 2주 재측정 0회 유지, 타 툴 정상 | config 되돌림 |
| **M1** | deepdive lifecycle 훅/플러그인화 | hook/plugin + 스펙 | 단계 기록 누락 0, enter/exit 자동 | 훅 비활성 |
| **M2** | obs 자동캡처(PostToolUse) | hook | 관찰 커버리지 ≥ 수동 대비, 중복 없음 | hook off |
| **M3** | Merge 파사드(memory/search/schema/plan) | 서버 코드+툴 스키마 | 툴 선택 정확도 유지/향상, 호출 감소 | 구툴 alias 유지 |
| **M4** | annotation + 재측정 | `readOnly/destructive/idempotent` | 2~4주 후 잔여 0회 툴 final 컷 | — |

## 5. 컷오버 계획 (`scripts/*` → `devforge`)

**공통 원칙**: 비파괴(구 유닛/이미지 `.disabled`·digest 보존), 병렬 검증 선행, 컴포넌트 1개씩, 롤백 = 역순.

| Phase | 대상 | devforge 목표 | 수락 | 롤백 |
|---|---|---|---|---|
| A | `container-devforge-mcp` | `adapters/driving/mcp` (FastMCP, **계약 12툴 보존** + `ingest`) | 클라이언트 무변경, `deepdive_*` E2E | 이전 이미지 |
| B | `devforge-turn-watcher` + 웹수집 | `domain/turn_collection` + `ingest` 수신 + provenance | 3s 폴링·중복 0·provenance 100% | 구 유닛 |
| C | `devforge-day-cycle` + worker | `application/orchestrator` + `pipeline_stages` (ADR-0005) | shadow 대조 diff=0, 2주 병렬 | `day_cycle.sh` |
| D | watchdog 3종 | `domain/watchdog` | 60s·sd_notify·incidents | 구 유닛 |
| E | inference | `domain/model_management`+`ports/container` | `devforge inference` 동등 | `model_ctl.sh` |
| F | notification/research/proxy | `adapters/driven/*` | 알림/리서치/프록시 동일 | 구 유닛 |
| G | backup/restore | `adapters/driven/storage` | 일일 백업+월간 복원 | 스크립트 유닛 |
| H | 컨테이너/이미지 | 단일 `devforge:latest`(digest 고정) | api/mcp/worker 기동 | digest 복원(<5분) |
| I | 정리 | `scripts/*` 이관, 문서 동기화 | `import lib` 실패, 롤백<5분 | git revert |

**비목표(유지)**: 골든 이미지(Azure repo), `system_sync.sh`(DuckDNS+autocommit).

## 6. 추출 아키텍처 (ADR-0005 요약)
`deterministic prefilter → 소형 로컬(constrained decoding) → hard만 클라우드 escalation`. 출력=strict schema+evidence binding+confidence gate+1회 재시도 후 quarantine. heavy는 **비동기 batch tier**, 서버 역할은 **검증/후보정**(NLI grounding·dedup·entity resolution). 측정: field-level precision/recall + escalation rate.

## 7. 통합 의사결정 (D1~D9)

> `결정:` 칸에 선택을 적으면 §4/§5 실행 우선순위가 확정된다. 상태: 대기 → 승인/보류.

**요약표**
| ID | 결정 | 선택지 | 권장 | 결정 |
|---|---|---|---|---|
| D1 | 추출 아키텍처(ADR-0005) | 승인/수정/보류 | 승인 | |
| D2 | MCP 전략(ADR-0006) | 승인/조정/보류 | 승인 | |
| D3 | 웹 수집(chrome-web) | 재가동/은퇴 | 재가동 | |
| D4 | 첫 적용 착수 | M0 착수/순서변경 | M0 착수 | |
| D5 | ingest 보안·프라이버시 | 노출/redaction/값 | loopback+bearer·적용·유지 | |
| D6 | 범위·일정 | worker/인력/컷오버 방식 | 흡수·축소·병렬 | |
| D7 | **MCP 최적화 옵션** | A/B/C/D/E | A 즉시 + B | |
| D8 | **훅/로직 전환 범위** | deepdive만/obs포함/최소 | deepdive+obs | |
| D9 | **Merge 범위** | memory/search/schema/plan 전체/일부 | 전체(4) | |

**현황 (2026-09-23, 근거 있는 것만 기록 — 나머지는 미결)**

| ID | 현황 | 근거 |
|---|---|---|
| D1 | **미결**(권장 승인) | ADR-0005 `Proposed`(미구현). Phase 3는 ADR-0005를 범위 외로 둠 |
| D2 | **승인(Effective at cutover)** | ADR-0006 `Accepted`(12툴 계약), `specs/mcp-contract.json` frozen |
| D3 | **재가동(운영 중)** | `chrome-web-llm` CLI 운영, 단 파이프라인 ingest 배선은 D4/D5 이후 |
| D4 | **미착수** | M0(allowlist 컷) 미실행 |
| D5 | **미결**(권장 loopback+bearer) | ingest 미복원 |
| D6 | **확정 = A** | `plans/phase3-plan.md`(D6=A: devforge는 embed 소유) |
| D7 | **미결**(권장 A 즉시+B) | MCP 최적화 미착수 |
| D8 | **미결**(권장 deepdive+obs) | 훅/로직 전환 미착수 |
| D9 | **미결**(권장 전체 4) | Merge 파사드 미착수 |

> 표준 정합 후속 항목(공급망·secretless·MCP audit·OTel/SLO 등)은 `plans/2026-standard-gap-remediation.md`로 분리. watchdog Gate4 재설계는 `plans/watchdog-standard-compliance.md`(정본).

**상세**
- **D1** 배경: 로컬 8B 배치 추출 → `reports/industry-standard-comparison` §5.3. 선택: 승인/수정/보류. 영향: Phase C·비용·`review_facts` 의미.
- **D2** 배경: 컷오버 시 **허용 12툴 계약** 보존 필요, `ingest` 미복원. 선택: 승인/조정/보류. 영향: Phase A·클라이언트 무변경.
- **D3** 배경: 웹 확장 아카이브·파이프라인 미연결. 선택: 재가동(확장+relay→`ingest`)/은퇴. 영향: 수집 커버리지·D5.
  - **(2026-09-20) 현황**: `chrome-web-llm` CLI **운영 중** — Qwen/DeepSeek 로그인·질의·세션 저장·모델 간 핸드오프(`runbooks/web-llm.md`). 단 대화의 파이프라인 수집(ingest) 배선은 **D4(`ingest`)+D5(보안) 이후 Phase B**에서 처리.
- **D4** 배경: 계약 고정됨. 선택: M0(`ingest`+provenance 또는 allowlist 컷) 착수/순서변경. 영향: 진행 속도.
- **D5** (5-1)노출 loopback/bearer/둘다 (5-2)시크릿 redaction (5-3)provenance 값(`chrome:*` 등). 권장: loopback+bearer·redaction 적용·값 유지.
- **D6** (6-1)worker 이관 흡수/별도 (6-2)2인/1인·범위축소 (6-3)병렬전환/즉시.
- **D7** MCP 최적화 옵션(§4.2). 권장 A 즉시 + B. C/D는 조건부.
- **D8** 훅/로직 전환 범위: deepdive 4종 / +obs_write / +start_lsp·detect. 권장 deepdive+obs.
- **D9** Merge 범위: memory·search·schema·plan(4건) / 일부. 권장 전체.

## 8. 리스크
| 리스크 | 완화 |
|---|---|
| 전환 직후 감시/수집 공백 | 비파괴 병렬 + 즉시 롤백 자산 보존 |
| MCP 계약 불일치 → 에이전트 장애 | Phase A 클라이언트 무변경 검증 |
| 훅 누락/중복(자동화) | 클라이언트별 훅(Claude Code 설정·opencode plugin), dedup |
| 추출 라우팅 품질 저하 | 골든셋 eval + escalation rate |
| day_cycle 로직 누락(455줄) | 행위 명세 + 2주 병렬 |
| 스키마 드리프트 | Alembic baseline + `alembic check` clean |

## 9. 근거 / 링크
- 계획: (구) `_archive/plans/cutover-remaining-plan.md`, (구) `_archive/plans/open-decisions.md` → **본 문서로 통합**
- 표준: `reports/industry-standard-comparison-20260914.md`
- 감사: `reports/mcp-tool-audit-20260914.md`
- ADR: `adr/0005-extraction-routing.md`, `adr/0006-mcp-tool-surface.md`
- 스펙: `specs/ingest-provenance.yaml`; 구조: `ARCHITECTURE.md`, `system-architecture.md` §3.5
