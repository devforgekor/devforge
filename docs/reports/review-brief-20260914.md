# DevForge 리뷰 브리프 (외부 에이전트용)

> Status: record · Date: 2026-09-14 · Owner: devforge
> 목적: 이 서버를 전혀 모르는 **외부 검토 에이전트**가 계획의 타당성을 판단할 수 있도록, 배경·현황·변화·검토대상·근거를 **자가완결**로 정리.
> 주의: repo 상대경로 링크는 외부에서 열리지 않으므로 핵심 사실은 본문에 임베드했다. 수치는 **2026-09-14 스냅샷**이다.

---

## 0. 리뷰 요청 (무엇을 봐 달라는가)
검토 대상 계획(첨부: **DevForge 최종 계획서**)에 대해 다음을 답해 달라:
1. 단계(컷오버 A~I, MCP 최적화 M0~M4)의 **누락·과잉·순서 오류**는?
2. **우선순위**는 합리적인가? (무엇부터 해야 하는가)
3. **리스크·롤백**이 충분한가? 놓친 실패 모드는?
4. **의사결정 D1~D9**(문서 §7)의 프레이밍·근거가 약하거나 잘못된 항목은?
5. 표준 대조/감사 결론에 **반론**이 있는가? 더 나은 대안은?

---

## 1. 시스템 한 줄 정체성
**DevForge** = Oracle Cloud(OCI, 도쿄 리전, ARM Ampere A1 4코어/22Gi, GPU 없음) **단일 서버**에서 돌아가는
**LLM 추론 + 데이터 파이프라인 + 웹앱 + 파일교환 통합 시스템**. AI 에이전트(Claude Code/opencode 등)의
대화·관찰·지식을 수집/가공해 MCP로 다시 노출하는 "개인 AI 개발 워크스테이션"이다.

## 2. 시스템 구조 (As-Is, 런타임)
- **OS/런타임**: Oracle Linux 9.7 (aarch64), Podman rootless(user `opc`), Caddy(rootful, host net).
- **DB**: PostgreSQL 16 (`devforge_app`) + pgvector + pg_trgm. 앱 소유 16테이블(대화 `turns`, `review_facts`, `observations`, `embeddings` …).
- **컨테이너(svc pod)**: `postgres`, `devforge-mcp`(FastMCP, :8000), `devforge-worker`(raw_consumer), `devforge-fastapi`(:8002 hub), `flaresolverr`. 추론은 동적 컨테이너 `devforge-inference`(llama.cpp :8080~8084).
- **systemd user 서비스(대표 22개)**: watchdog(3), turn-watcher, day-cycle, system-sync, backup, restore-test, dev-poll, tg-webhook, activity-summarizer, weekly-enrich-rebuild, proxies(anthropic/openrouter/gemini/rate-limiter 5종), golden-image(2), etc.
- **포트**: Caddy 80/443, FastAPI hub 8002, MCP 8000, blob explorer 8085, inference 8080~8084.
- **데이터 흐름**: `turn_watcher`(3초 폴링, 로컬 에이전트 로그 5종 파싱) → `turns`(raw) → `raw_consumer`(worker) → `pending` → `day_cycle.sh`(clean→scan→extract→verify→enrich→embed) → MCP 노출(`fact_*`,`mem_*`,`obs_*`,`search_*`).

## 3. 코드 구조 (As-Is) — 레거시 vs 신규
- **레거시(라이브)**: `scripts/` 평면 배치(진입점 50+, `scripts/lib/` 28개 서브모듈). systemd/Quadlet가 이걸 실행.
- **신규(리팩토링)**: `src/devforge/` — src-layout + DDD + Ports & Adapters. 단일 CLI `devforge`(`pyproject.toml [project.scripts]`).

**구현 성숙도(신규 패키지)**
| 계층 | 상태 |
|---|---|
| `core/{config,logging}`, `ports/extract`, `domain/models` | 구현 |
| `adapters/driven/{llm/local_adapter, storage/database_gateway·extract_adapter}` | 구현 |
| `adapters/driving/{api, mcp(SSE 5툴), cli_cmds}` | 부분 |
| `application/extract_pipeline`, `pipeline_stages/extract` | 부분(추출만) |
| `domain/{watchdog,turn_collection,model_management,pipeline}`, `adapters/driven/{notification,research,proxy_utils}` | **미구현(0줄 stub)** |

→ **즉 리팩토링은 "골격 + 추출 파이프라인"까지만 되어 있고, 라이브 서비스는 아직 레거시 `scripts/`가 담당.**

## 4. 무엇이 변했나 (2026-09-13~14 변화 이력)
1. **신규 패키지 스캐폴딩**(`src/devforge`)과 `devforge` CLI 동작화. 결함 수정: `domain/models` `TypeDecorator` 누락(패키지 import 불가), `cli.py` IndentationError, CLI 중복 구현 배선.
2. **쓰기 경로 버그 2건 수정**: `on_conflict_do_update`를 generic `insert`로 호출, `stmt.inserted`→`stmt.excluded`.
3. **DB 기반 정비**: `docs/specs/schema.sql`을 ORM과 일치 재생성(16테이블), `turns.source` 컬럼 추가(라이브엔 없었음), **Alembic baseline**(`alembic stamp head`, `alembic check` clean), **ORM↔live 재정합**(BIGINT PK·nullability·FK ondelete·index/opclass).
4. **Shadow DB 적용**: `devforge_shadow`(검증용) + `turns_shadow` 뷰.
5. **CI/빌드 정비**: `README.md` 생성(pyproject 참조), `Dockerfile` 재작성, 의존성 누락(`structlog`,`pyyaml`) 추가, ruff/format/mypy/import-linter/pytest green.
6. **로그 정비**: structlog 이중 출력 제거(1회 렌더).
7. **은퇴(superseded/제거)**: 문서생성기 `gen_architecture.py`(신뢰 불가) → `_archive`, 호출부(system_sync/day_cycle/daily-structure) 제거; **Gemini 에이전트 세션 로직** → `_archive`(서비스 disable). *(Gemini 모델 프록시·세션 파서는 유지)*
8. **문서 정비(SSOT)**: 리팩토링 문서군(ARCHITECTURE/MIGRATION_GUIDE/API_REFERENCE/OPERATIONS_GUIDE/ADR 0001~0006) 작성, INDEX/CONVENTIONS/CLAUDE.yaml/blueprint/handover 동기화, 계획 문서 단일 통합(`final-plan.md`).
9. **E2E 검증**: 실 DB에서 읽기(`devforge pipeline status`)·쓰기(테스트 turn → facts 저장) 확인, replay fixture 복구.

> 요약: **"신규 패키지 골격 + 안전장치(DB/CI/문서) 완성, 라이브 컷오버는 미착수."**

## 5. 검토 대상 계획 (첨부 문서)
**DevForge 최종 계획서** = 컷오버(A~I) + MCP 툴 최적화(검토안 A~E) + 통합 의사결정(D1~D9).
핵심 전제: 라이브 유닛 22개 + 컨테이너 3개가 `scripts/*`에 의존하고, 신규 패키지는 일부만 구현 → **전면 컷오버는 도메인 구현 후**, 지금은 **비파괴 병렬 검증**과 **저위험 항목(MCP allowlist 컷, `ingest`/provenance)** 부터.

## 6. 근거 요약

### 6.1 업계 표준 5계층 대조
| 계층 | 표준 | 현재 | 판정 |
|---|---|---|---|
| Capture | hook 결정론 + 확장/relay(웹) | 로컬 파서 폴링, 웹 미연결 | 개선 필요 |
| Ingestion | hook→queue→importer | watcher→raw→worker | 부분 |
| Storage/Retrieval | FTS+vector+RRF, L0~L3 | Postgres+pgvector+FTS5+RRF | 일치(계층 일부) |
| Extraction | deterministic→LLM, 구조화, evidence, hybrid | 로컬 8B 배치 | 이탈 |
| Serving(MCP) | 서버당 5~15, 근접중복 merge | 로드 33(allowlist), 0회 9 | 개선 필요 |

### 6.2 MCP 툴 감사 (30일 실사용)
- 활성 MCP **서버 4개**(devforge-mcp·yggdrasil·lsp·opencode-db). opencode allowlist로 **로드 33툴**.
- **30일 0회 9개**(lsp proxy_artifact 3·find_symbol·inspect_symbol·get_symbol_source·detect_lsp_servers, yggdrasil list_plans 등).
- 판정: Remove(9+) / Merge(deepdive 4→1, mem+obs→2, search→1, schema→1) → **목표 33→약 12**.
- 참고: 빌트인(bash/read)이 MCP보다 압도적 → LSP read 계열이 불필요.

## 7. 용어집 (외부인용)
- **Track A/B**: A=리팩토링(현재), B=클라우드 LLM 공급자 추상화(별도, 미착수).
- **Ports & Adapters**: 도메인(포트)과 외부기술(어댑터) 분리.
- **shadow DB**: 프로덕션과 분리된 검증용 스키마(`devforge_shadow`).
- **cutover**: 라이브 실행 경로를 `scripts/*` → `devforge`로 전환.
- **MCP**: Model Context Protocol(에이전트가 도구를 호출하는 표준).

---

## 8. 검토 에이전트용 프롬프트 (붙여넣기용)
```
너는 DevForge 서버를 전혀 모르는 외부 아키텍처 리뷰어다.
첨부한 "리뷰 브리프"(시스템 구조·변화 이력·근거 요약)와 "최종 계획서"만 근거로,
계획을 비판적으로 검토하라. 추측과 사전지식은 배제하고 문서 근거만 인용하라.

출력 형식:
1) 요약 판정(승인/조건부/보류) + 한 줄 이유
2) 계획 단계(A~I, M0~M4)별 누락·과잉·순서 문제 (표)
3) 우선순위 재제안(무엇부터)
4) 리스크·롤백 갭
5) 의사결정 D1~D9 프레이밍 약점
6) 반론/대안
7) 더 필요한 정보(질문 목록)
```
