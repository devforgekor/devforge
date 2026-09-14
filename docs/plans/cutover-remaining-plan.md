# Cutover 잔여 계획서 — `scripts/*` → `devforge` 전환

> Status: proposed · Date: 2026-09-14 · Owner: devforge · Related: `docs/REFACTORING_PLAN.md`, `docs/ARCHITECTURE.md`, `docs/MIGRATION_GUIDE.md`, `docs/reports/industry-standard-comparison-20260914.md`
> 목적: 리팩토링된 `src/devforge` 패키지로 **라이브 서비스를 실제 전환**하기 위한 잔여 작업을 단계·수락기준·롤백까지 정의한다.
> 업계 표준 대조·근거: `docs/reports/industry-standard-comparison-20260914.md` (ADR-0005/0006).

---

## 1. 배경 (왜 별도 계획인가)

`REFACTORING_PLAN.md` v1.4의 Phase 0~1(기반/추출 파이프라인)은 완료됐고, Alembic 베이스라인·ORM↔live
재정합·E2E 읽기/쓰기 검증까지 끝났다. 그러나 라이브는 아직 **systemd 유닛 22개 + Quadlet 컨테이너 3개**가
`scripts/*`를 `ExecStart`로 사용하며, `devforge`에는 이들 중 **대부분의 도메인이 미구현(0줄 stub)**이다.
이 문서는 "무엇을 어떤 순서로 안전하게 전환하는가"를 정의한다.

## 2. 현황 (As-Is) — 라이브 의존성 vs 구현

### 2.1 전환 대상 (라이브 → 목표 모듈)

| 라이브 유닛/컨테이너 | 현재 실행 | devforge 목표 | 상태 |
|---|---|---|---|
| `container-devforge-mcp` | `scripts/mcp_server.py` (FastMCP Streamable HTTP, :8000) | `adapters/driving/mcp` | 부분(SSE·툴 5개만) |
| `container-devforge-fastapi` | `scripts/devforge_fastapi/app.py` (hub :8002, blob :8085) | `adapters/driving/api` | 부분(설정/health/extract만) |
| `container-devforge-worker` | `worker_supervisor.py` (raw_consumer Pass2/3) | `application`/`pipeline_stages` | 미구현 |
| `devforge-turn-watcher` | `scripts/turn_watcher.py` | `domain/turn_collection` | 미구현(0줄) |
| `devforge-day-cycle` | `scripts/day_cycle.sh` | `application/orchestrator`+`pipeline_stages` | 부분(extract만) |
| `devforge-watchdog`(+`-liveness`,`-failed`) | `scripts/watchdog.py` | `domain/watchdog` | 미구현(0줄) |
| inference 관리(quadlet, `model_ctl.sh`) | `scripts/lib/pod_manager` | `domain/model_management`+CLI | 부분(CLI wrapper만) |
| `devforge-backup`,`-restore-test` | `osync_backup.py`,`osync_restore_test.py` | `adapters/driven/storage` | 미구현 |
| `devforge-system-sync`,`-daily-structure` | `system_sync.sh` | ops(DuckDNS+autocommit 유지) | 범위 밖(문서생성기 제거됨) |
| `devforge-dev-poll` | `cli.py dev poll` | `application/issue_collector` | 미구현 |
| `devforge-tg-webhook`,`activity-summarizer` | `lib/tg_webhook.py`,`activity_summarizer.py` | `adapters/driven/notification` | 미구현(0줄) |
| proxies 5종(anthropic/openrouter/gemini/rate-limiter/free-models) | `scripts/proxies/*`,`or_rate_limiter.py` | `adapters/driven/proxy_utils` | 미구현(0줄) |
| `weekly-enrich-rebuild` | `weekly_enrich_rebuild.sh` | `pipeline_stages/enrich` | 미구현 |
| golden-image 2종, `gemini-session` | `scripts/golden_image/*`,`gemini_session_start.sh` | — | 범위 밖(유지) |

> 판정 기준: `domain/{watchdog,turn_collection,model_management,pipeline,pipeline/stages}`,
> `adapters/driven/{notification,research,proxy_utils}` = **0줄(미구현)**.

### 2.2 이미 갖춘 것 (재사용)
- `core/config.py`(`ConfigRegistry`), `core/logging.py`(단일 렌더), `domain/models.py`(live 정합), `ports/extract.py`
- `adapters/driven/llm/local_adapter.py`, `adapters/driven/storage/{database_gateway,extract_adapter}.py`
- `application/extract_pipeline.py`, `pipeline_stages/extract/edc.py`, `adapters/driving/{api,mcp,cli_cmds}`
- Alembic 베이스라인(head), ORM↔live `alembic check` clean, `sql/shadow_schema.sql`

## 3. 범위

**포함**: 라이브 22 유닛/3 컨테이너의 `devforge` 전환(구현 → 병렬 검증 → 스위치 → 롤백).
**비목표(유지)**: `system_sync.sh`(DuckDNS+autocommit), `golden_image/*`(Azure 별도 repo).
**제거(2026-09-14)**: `gen_architecture.py`(문서생성기, 신뢰 불가) 및 Gemini 에이전트 세션 로직(`gemini_session_start.sh`+`gemini_core/` 등) — `_archive/`로 이동.
`gemini_session_start.sh`(운영 래퍼). Track B(클라우드 공급자)는 `docs/LLM_PROVIDER_PLAN.md`로 분리.

## 4. 공통 전환 원칙

1. **비파괴**: 기존 유닛/이미지는 **삭제하지 않고** `.disabled`/이전 태그로 보존(롤백 자산).
2. **병렬 검증 먼저**: 신규 포트/스키마(`devforge_shadow`)에서 최소 1주(또는 n≥14 사이클) 대조 후 스위치.
3. **컴포넌트 1개씩**: 한 번에 하나만 전환. 각 단계 종료 시 게이트(`ruff`/`mypy`/`import-linter`/pytest/E2E) green.
4. **Rollback 기본**: 전환은 `enable --now new` → `disable --now old` 순서. 실패 시 즉시 역순.
5. **feature flag**: `DEVFORGE_*` env로 신/구 경로 토글.

## 5. 단계 계획

각 단계: **구현(모듈) → 수락기준 → 전환 절차 → 롤백**.

### Phase A — MCP 정합/전환 (선행 필수)
- **구현**: `adapters/driving/mcp`를 라이브와 동일 계약으로 정합 — FastMCP Streamable HTTP + **네임스페이스 8종**
  (knowledge/memory/pipeline/inference/actions/watchdog/deepdive). `mcp/server.py` 자체 SSE 흡수.
- **툴 표면(ADR-0006)**: 항상-로딩(always-on) **10~20 툴**로 제한, **`search_tools` 점진공개**로 long-tail 노출.
  현 `devforge-mcp` 25+ 툴 → 네임스페이스 분할. 툴 description에 **"사용 조건/비사용 조건(경계)"** 명시.
- **`ingest` 복원(ADR-0006)**: 배치 대화 수신을 **MCP `ingest` + `POST /api/v1/ingest`** 양쪽 제공. `source`/`agent` provenance 기록.
- **수락**: 기존 에이전트 MCP 클라이언트 설정 **무변경** 동작. `deepdive_step_*` E2E 재현. 툴 정의 토큰 < 컨텍스트 5%.
- **전환**: `container-devforge-mcp` 이미지 → `localhost/devforge:latest`(`Exec=devforge mcp serve`). 이전 이미지 digest 보존.
- **롤백**: 이전 이미지 태그로 `systemctl --user restart container-devforge-mcp`.

### Phase B — turn_collection + 웹 수집(ingest) 전환
- **구현(로컬)**: `domain/turn_collection`(파서 5종 + watcher + checkpoint) 포팅. `scripts/lib/parsers/*` 이관.
- **구현(웹, ADR-0006)**: Chrome 확장 + Native Messaging + loopback relay(chrome-web-llm) → `ingest` 수신 경로 편입.
  `POST /api/v1/ingest`(정본) / MCP `ingest`(호환) 중 택1 정본화.
- **provenance**: `turns.source`/`agent` 표준화(`chrome:qwen`, `chrome:deepseek`, `claude-code`, `opencode` …). 현재 전량 `unknown` 해소.
- **수락**: 3초 폴링, `turns`(raw) 삽입, `collect_checkpoint.json` 호환, 웹 대화 유입 E2E, 24h 무중단/중복 0, provenance 100%.
- **전환**: `devforge-turn-watcher.service` ExecStart → `devforge turn-watch`(신규 CLI). 구 유닛 `.disabled`.
- **롤백**: 구 유닛 re-enable.

### Phase C — pipeline 도메인 + 오케스트레이터 (day_cycle)
- **구현**: `pipeline_stages/{text_clean,entity_scan,extract,verify,enrich,embed}` + `application/orchestrator.py`
  (`day_cycle.sh` 상태머신·예산·순서 이관) + `raw_consumer`.
- **추출 라우팅(ADR-0005)**: `deterministic prefilter → 소형 로컬(constrained decoding) → hard만 클라우드 escalation`.
  구조화(strict schema)+evidence binding+confidence gate+`1회 재시도 후 quarantine`. heavy 추출은 **비동기 batch tier**로.
  서버는 **검증/후보정**(NLI grounding/dedup/entity resolution) 중심으로 재배치.
- **수락**: `devforge pipeline orchestrate`가 전 단계 수행. shadow DB + replay로 구/신 대조(결정론 diff=0).
  field-level precision/recall + **escalation rate** 측정, 골든셋 드리프트 감지. `day_cycle.sh`와 2주 병렬.
- **전환**: `devforge-day-cycle.service` ExecStart → `devforge pipeline day-cycle`; `container-devforge-worker` 이미지 교체.
- **롤백**: `day_cycle.sh` 유닛 re-enable + worker 이전 이미지.

### Phase D — watchdog 도메인
- **구현**: `domain/watchdog`(checker/state/recovery/incidents/notifier/orchestrator) 포팅.
- **수락**: 60초 루프, `sd_notify`(WatchdogSec), graduated recovery, `watchdog_incidents` 기록, liveness/외부핑.
- **전환**: `devforge-watchdog{,-liveness,-failed}.service` ExecStart → `devforge watchdog [--liveness-check|--notify-failure]`.
- **롤백**: 구 유닛 re-enable (감시 공백 최소화 위해 `systemctl restart` 즉시).

### Phase E — model_management + inference
- **구현**: `domain/model_management`(MODEL_REGISTRY 이관) + `ports/container.py` + `adapters/driven/container`(Podman subprocess).
- **수락**: `devforge inference switch|status|ensure`가 실제 모델 기동/정지/헬스체크 수행(`model_ctl.sh` 동등).
- **전환**: quadlet inference 컨테이너 엔트리/`day_cycle`의 모델 관리 경로 교체.
- **롤백**: `model_ctl.sh` 경로 복원.

### Phase F — driven 어댑터 (notification/research/proxy_utils)
- **구현**: Slack/Telegram(Apprise), exa/context7/web, 게이트웨이(OpenRouter 등) 이관.
- **수락**: 알림 수신, 리서치 호출, 프록시 라우팅이 기존과 동일. `devforge-tg-webhook`,`activity-summarizer`,proxies 5종 전환.
- **롤백**: 각 유닛 re-enable.

### Phase G — 운영/백업
- **구현**: `adapters/driven/storage`에 OCI 백업/복원(`osync_backup`/`osync_restore_test`) 이관 + watchdog 연계.
- **수락**: 일일 백업 + 월간 복원 검증 통과. `devforge-backup`,`-restore-test` 전환.
- **롤백**: 스크립트 유닛 복원.
- (참고) `gen_architecture`는 **2026-09-14 제거됨**, `system_sync`는 유지(DuckDNS+autocommit).

### Phase H — 컨테이너/이미지 최종화
- **구현**: 단일 `localhost/devforge:latest`(멀티 진입점) + Quadlet digest 고정.
- **수락**: `devforge` 이미지로 api/mcp/worker 기동, Caddy 라우트 불변.
- **롤백**: digest 고정으로 이전 이미지 즉시 복원(<5분).

### Phase I — 최종 정리
- **구현**: 컷오버 완료분 `scripts/*` 제거/이관(`_archive/`), 문서 동기화(`ARCHITECTURE`/`OPERATIONS`/`INDEX`), 롤백 훈련.
- **수락**: `python -c "import lib"` 실패(정리 확인), 잔여 shim 0, 롤백 <5분.
- **롤백**: git revert + 이전 이미지.

## 6. 단계 의존성

```
A(MCP) ──┐
B(turn_collection) ──→ C(pipeline/day_cycle) ──→ H(container)
D(watchdog) ──────────┘
E(inference) ─────────┘
F(adapters) ──────────┘
G(ops/backup) ────────┘
모든 단계 → I(최종 정리)
```

## 7. 검증 게이트 (각 단계 공통)

- 정적: `ruff check src/ tests/`, `ruff format --check src/`, `mypy src/ --strict`, `lint-imports`
- 테스트: `pytest tests/test_characterization.py tests/test_integration.py` (+ 단계별 특성화 테스트 신규)
- E2E: `--pod svc` 내부에서 실 DB read/write + replay fixture
- 대조: shadow(`devforge_shadow`) 구/신 diff

## 8. 리스크

| 리스크 | 완화 |
|---|---|
| 전환 직후 감시/수집 공백 | 비파괴 병렬 검증 + 즉시 롤백 유닛 보존 |
| MCP 프로토콜/툴 불일치 → 에이전트 장애 | Phase A에서 클라이언트 무변경 검증 필수 |
| day_cycle 로직 누락(455줄) | 행위 명세 + 2주 병렬 대조 |
| DB 스키마 드리프트 | Alembic baseline + `alembic check` clean 유지 |
| 컨테이너 이미지 회귀 | digest 고정 + <5분 롤백 |
| 도메인 미구현 상태 전환 시도 | 단계별 "구현→수락" 통과 전 전환 금지 |

## 9. 미결 (Open questions)

1. **MCP 전략**: FastMCP 채택(라이브 정합) + **툴 10~20 + `search_tools` 점진공개**(ADR-0006, proposed). SSE 유지안은 폐기.
2. **shadow DB 적용**: ✅ **적용 완료**(2026-09-14, `devforge_shadow` + `turns_shadow` 뷰 + `review_facts_shadow`).
3. **worker_supervisor 이관 범위**: `container-devforge-worker`를 application 계층으로 흡수(예정).
4. **범위 확정**: 골든 이미지=범위 밖 유지. `gen_architecture`=**제거됨**. Gemini 에이전트 세션=**제거됨**.
5. **일정/인력**: `REFACTORING_PLAN` v1.4(2인·14주) 가정과 실제 인력 정합(보류).
6. **웹 수집(chrome-web) 편입**: 확장+relay → `ingest` 수신 경로 편입 + provenance 표준(ADR-0006, proposed).

## 10. 근거

- `docs/REFACTORING_PLAN.md` v1.4 (Phase 0~8 정의)
- `docs/reports/industry-standard-comparison-20260914.md` (업계 표준 5계층 대조)
- `docs/adr/0005-extraction-routing.md`, `docs/adr/0006-mcp-tool-surface.md`
- `docs/ARCHITECTURE.md` (패키지/계층), `docs/system-architecture.md` §3.5 (컷오버 미완 상태)
- 라이브 인벤토리: `systemctl --user list-units`/`~/.config/containers/systemd/*.container`
- `handover.yaml` known_issues (cutover pending, Alembic baseline, ORM reconcile)
