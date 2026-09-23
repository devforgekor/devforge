# DevForge — Code Architecture

> Status: active · Date: 2026-09-23 · Owner: devforge · Related: `docs/system-architecture.md`, `docs/REFACTORING_PLAN.md`, `docs/refactoring/REFACTORING_STATUS.yaml`
> 코드 구조(패키지·계층·의존성 규칙)의 정본. 런타임/인프라 구조는 `docs/system-architecture.md`를 본다.

---

## 1. 개요

DevForge 서버 코드는 **src-layout 패키지** `src/devforge/`로 구성되며,
**Domain-Driven Design** 경계와 **Ports & Adapters(Hexagonal)** 패턴을 따른다.
레거시 `scripts/`(진입점·서비스·`lib/`)는 컷오버 전까지 병존한다.

- 설치: `pip install -e ".[dev]"`
- 진입점: `devforge` CLI (`pyproject.toml [project.scripts]`)
- 구조 SSOT: `docs/architecture/code-structure.yaml` (수동 유지 — 생성기 `gen_architecture.py` 은퇴 2026-09-14; `tests/unit/test_code_structure.py`가 정합 검증)

## 2. 패키지 레이아웃

```
src/devforge/
├── cli.py                       # Typer 단일 진입점 (composition root)
├── core/                        # config(ConfigRegistry+Paths), logging, paths, exceptions
├── ports/                       # 인터페이스 (Protocol/ABC)
│   ├── extract.py               # LLMPort, ExtractPort, TurnRepository, ObservationRepository
│   └── container, health_check, heartbeat, incident_repository,
│       notification, recovery, state_persistence, types
├── domain/                      # 비즈니스 도메인 / SQLAlchemy 모델
│   ├── models.py                # 앱 소유 16개 테이블 (docs/specs/schema.sql 대응)
│   ├── model_management/        # 모델 메타데이터 (GGUF)
│   ├── pipeline/ (stages/)      # 순수 파이프라인 로직 (extract 구현; enrich/embed/review 예정)
│   ├── turn_collection/         # turn 수집
│   └── watchdog/                # monitoring(tracker,backoff) / orchestration / recovery(graduation,strategies)
├── adapters/
│   ├── driven/                  # 외부 시스템을 호출하는 어댑터
│   │   ├── llm/local_adapter.py            # llama.cpp HTTP(:8080-8085), LocalLLMAdapter
│   │   ├── storage/                        # database_gateway, extract_adapter, incident_pg, state_json, heartbeat_pg
│   │   ├── health/                         # ebook, llm, pipeline, svcpod, systemd, system
│   │   ├── container/podman_adapter.py     # Podman 제어
│   │   ├── recovery/systemd_recovery.py    # systemd 복구
│   │   └── notification/ research/ proxy_utils/
│   └── driving/                 # 외부 입력을 받는 어댑터
│       ├── api/                      # FastAPI HTTP API
│       ├── mcp/                      # MCP SSE 서버 + Tools
│       └── cli_cmds/                 # CLI 서브커맨드 (inference, mcp)
├── application/                 # 애플리케이션 서비스 / 오케스트레이션
│   ├── extract_pipeline.py      # ExtractPipeline(+ ExtractResult)
│   ├── orchestrator.py          # PipelineOrchestrator(+ BudgetManager) 골격
│   └── watchdog_service.py      # watchdog 서비스 (create_watchdog_service / run_cycle)
└── pipeline_stages/             # 파이프라인 실행 모듈
    └── extract/edc.py           # extract 프롬프트/파서
```

## 3. 의존성 규칙 (import-linter)

상위 계층만 하위 계층을 import할 수 있다. CI(`lint-imports`)가 강제한다.

```
application      → (모두)
pipeline_stages  → adapters, ports, core, domain
adapters         → ports, core, domain
domain           → core
core             → (독립)
ports            → (최하위, 의존성 없음)
```

- `core`는 절대 `adapters`를 import하지 않는다 (순환 방지).
- `cli.py`(composition root)만이 `application`을 import하는 진입점이다.

## 3.1 CI (`.github/workflows/ci.yml`)

`lint → type-check → architecture → test → build` 순서. 로컬 검증 명령은 CI와 동일하게 맞춘다:

- `lint`: `ruff check src/ tests/` (**tests/ 포함**) + `ruff format --check src/`
- `type-check`: `mypy src/ --ignore-missing-imports`
- `architecture`: `lint-imports` (4 contracts KEPT)
- `test`: `pytest tests/test_characterization.py tests/test_integration.py` (Postgres 서비스)
- `build`: GHCR push (`permissions: packages: write` 필요)

## 4. 진입점 / 계약

| 종류 | 정의 | 비고 |
|---|---|---|
| CLI | `pyproject.toml [project.scripts] devforge = "devforge.cli:app"` | `docs/API_REFERENCE.md` |
| HTTP | `devforge.adapters.driving.api.app:app` (uvicorn, :8000) | FastAPI |
| MCP | `devforge.adapters.driving.mcp.server:app` (SSE, :8100) | `devforge mcp serve` |
| LLM 포트 | `ports/extract.py::LLMPort` | 구현: `adapters/driven/llm/local_adapter.py` |
| 공급자 선택 | `ConfigRegistry.llm_provider` (`DEVFORGE_LLM_PROVIDER`) | Track A = `local` |

## 5. 컷오버 상태

`devforge` 패키지는 설치·동작하지만, **라이브 systemd 유닛/Quadlet의 `ExecStart`는 아직
레거시 `scripts/*`를 가리킨다** (2026-09-23 기준 라이브 유닛 30개). 진행 현황:

- Phase 0/1/1.5 **완료**, Phase 2(watchdog) **shadow-run(2.5)** — `devforge-watchdog-v2.service`가
  legacy `devforge-watchdog.service`와 병행 실행 중.
- 컷오버 전략: `docs/REFACTORING_PLAN.md`(v1.5) §5 / `docs/plans/final-plan.md` §5(Phase A~I).
- 런타임 인벤토리: `docs/system-architecture.md` §3.5.
