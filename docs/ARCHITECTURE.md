# DevForge — Code Architecture

> Status: active · Date: 2026-09-14 · Owner: devforge · Related: `docs/system-architecture.md`, `docs/REFACTORING_PLAN.md`
> 코드 구조(패키지·계층·의존성 규칙)의 정본. 런타임/인프라 구조는 `docs/system-architecture.md`를 본다.

---

## 1. 개요

DevForge 서버 코드는 **src-layout 패키지** `src/devforge/`로 구성되며,
**Domain-Driven Design** 경계와 **Ports & Adapters(Hexagonal)** 패턴을 따른다.
레거시 `scripts/`(진입점·서비스·`lib/`)는 컷오버 전까지 병존한다.

- 설치: `pip install -e ".[dev]"`
- 진입점: `devforge` CLI (`pyproject.toml [project.scripts]`)
- 구조 SSOT: `docs/architecture/code-structure.yaml` (자동, `scripts/` + `src/devforge/` 스캔)

## 2. 패키지 레이아웃

```
src/devforge/
├── cli.py                       # Typer 단일 진입점 (composition root)
├── core/                        # 공통 인프라
│   ├── config.py                # ConfigRegistry (5개 소스 통합) + Paths
│   └── logging.py               # structlog 기반 구조화 로깅
├── ports/                       # 인터페이스 (Protocol/ABC)
│   └── extract.py               # LLMPort, ExtractPort, TurnRepository, ObservationRepository
├── domain/                      # 비즈니스 도메인 / SQLAlchemy 모델
│   ├── models.py                # 앱 소유 16개 테이블 (docs/specs/schema.sql 대응)
│   ├── model_management/        # (stub) 모델 메타데이터
│   ├── pipeline/                # (stub) 순수 파이프라인 로직
│   ├── turn_collection/         # (stub) turn 수집
│   └── watchdog/                # (stub) 헬스체크/복구
├── adapters/
│   ├── driven/                  # 외부 시스템을 호출하는 어댑터
│   │   ├── llm/local_adapter.py      # llama.cpp HTTP(:8080-8085), LocalLLMAdapter
│   │   ├── storage/database_gateway.py, extract_adapter.py
│   │   ├── notification/ research/ proxy_utils/   # (stub, Phase 8)
│   └── driving/                 # 외부 입력을 받는 어댑터
│       ├── api/app.py                # FastAPI HTTP API
│       ├── mcp/server.py             # MCP SSE 서버 + Tools
│       └── cli_cmds/                 # CLI 서브커맨드 (inference, mcp)
├── application/                 # 애플리케이션 서비스 / 오케스트레이션
│   └── extract_pipeline.py      # ExtractPipeline(+ ExtractResult)
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
레거시 `scripts/*`를 가리킨다** (2026-09-14 기준). 컷오버 전략은
`docs/REFACTORING_PLAN.md`를 따르며, 런타임 인벤토리는 `docs/system-architecture.md` §3.5를 참조한다.
