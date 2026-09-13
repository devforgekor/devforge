# DevForge 서버 리팩토링 종합 계획서

> **작성일**: 2026-09-13  
> **버전**: 1.0  
> **작성자**: DevForge Team  
> **상태**: Draft — 검토 후 확정

---

## 1. 개요

### 1.1 목적
현재 `scripts/` 루트에 평평하게 배치된 50+ 진입점 스크립트와 28개 서브모듈로 구성된 `scripts/lib/`을 **업계 표준 Python 패키지 구조(src-layout + Domain-Driven Design)**로 재구성하여, AI 에이전트와 사람이 모두 탐색하기 쉬운 코드베이스를 만든다.

### 1.2 배경
- **현재 문제**: 진입점 분산(`cli.py`, `watchdog.py`, `turn_watcher.py`, `mcp_server.py`, `day_cycle.sh` 등), 순환 참조 위험, 하드코딩된 경로, 설정 파일 5개 분산, Shell/Python 혼재
- **목표 구조**: 설치 가능한 패키지(`pip install -e .`), 단일 진입점(`devforge` CLI), 도메인별 경계 명확화, 컨테이너 친화적 빌드/실행

### 1.3 범위
- **대상**: `/opt/projects/server/scripts/`, `/opt/projects/server/docs/` (문서 구조 유지)
- **제외**: `/opt/workspace/` 외부 워크스페이스, `_archive/` 과거 산출물
- **일정**: 8주 (Phase 0~7), 각 Phase 1주 단위

---

## 2. 현재 시스템 분석 (As-Is)

### 2.1 디렉토리 구조 현황
```
/opt/projects/server/
├── scripts/                    # 루트: 50+ 파일 평평 배치
│   ├── cli.py (74KB)           # 메인 CLI지만 다른 진입점들과 동급
│   ├── watchdog.py (277B)      # 래퍼, 실제 로직은 lib/watchdog/
│   ├── turn_watcher.py (13KB)  # 파이프라인 진입점
│   ├── mcp_server.py (56KB)    # MCP 서버
│   ├── day_cycle.sh (19KB)     # 셸 오케스트레이터
│   ├── *.bak, *.backup, *.old  # 중복/백업 10개+
│   ├── golden_image -> ...     # 심볼릭 링크 (도구 깨짐)
│   └── lib/ (28개 서브디렉토리) # "유틸리티 창고", 도메인 경계 불명확
├── docs/                       # 문서 (유지)
├── containers/                 # 컨테이너 정의
├── config/                     # 설정 템플릿
└── pyproject.toml 없음         # 설정 분산 (ruff.toml, pytest.ini 등)
```

### 2.2 핵심 컴포넌트 런타임 동작

| 컴포넌트 | 현재 위치 | 진입점 | 상태 관리 | 의존성 |
|----------|-----------|--------|-----------|--------|
| **Turn Collection** | `turn_watcher.py` + `lib/parsers/` | systemd service | `collect_checkpoint.json` | DB, 파일시스템 |
| **Pipeline (day_cycle)** | `day_cycle.sh` + `pipelines/*.py` | systemd timer | `pipeline_state` (DB) | Inference, DB |
| **Inference Mgmt** | `lib/pod_manager/` | `cli.py` 내부 함수 | `current-mode-inference.env` | Podman, 모델 파일 |
| **Watchdog** | `watchdog.py` → `lib/watchdog/orchestrator.py` | systemd service | 메모리 + `watchdog_liveness` 파일 | systemd, Podman, Slack |
| **MCP Server** | `mcp_server.py` | systemd service | DB (stateless) | Inference(:8080-8084), DB |
| **Config** | 5개 파일 분산 | - | - | - |

### 2.3 데이터 플로우 (현재)
```
AI Agents → turn_watcher (3s poll) → turns.raw
    → raw_consumer → turns.pending
    → day_cycle.sh 순차 실행:
        batching → cleaned (text_clean)
        → scanned (entity_scan)
        → verified (extract + NLI)
        → enriched (enrich + grounding)
        → embedded (embed_batch)
    → MCP Tools 노출 (fact_search, mem_search 등)
```

### 2.4 주요 기술적 부채
| 구분 | 내용 | 영향도 |
|------|------|--------|
| **진입점 분산** | 5개 메인 진입점이 루트에 동급 배치 | AI 에이전트 탐색 실패 |
| **설정 분산** | `state.yaml`, `CLAUDE.yaml`, `secrets.env`, `current-mode-inference.env`, `current-system-mode.env` | 환경별 설정 관리 난이도 ↑ |
| **Shell/Python 혼재** | `day_cycle.sh` 455줄 + Python 파이프라인 | 테스트/디버깅/타입힌트 불가 |
| **하드코딩 경로** | `/opt/ai_data/...`, `/opt/projects/server/...` 40+ 곳 | 컨테이너 이식성 없음 |
| **순환 참조 위험** | `cli.py` → `lib.*` → `scripts.*` 양방향 | LSP 심볼 해결 실패 |
| **중복 파일** | `extract_llm.py.backup`, `.dual4b.bak`, `pipeline_common.py.bak` | 에이전트 혼란 |
| **심볼릭 링크** | `golden_image → /opt/workspace/...` | Git/LSP/도구 체인 깨짐 |

---

## 3. 목표 아키텍처 (To-Be)

### 3.1 설계 원칙 (업계 표준 준수)

| 원칙 | 참조 표준 | 적용 방식 |
|------|-----------|-----------|
| **src-layout** | Python Packaging Authority | `src/devforge/` 패키지, `pip install -e .` |
| **Domain-Driven Design** | Evans DDD / Microsoft eShopOnContainers | Bounded Contexts = `domain/` 서브패키지 |
| **Ports & Adapters (Hexagonal)** | Alistair Cockburn | `interfaces/` = Adapters, `domain/` = Core |
| **Single Entry Point** | Click/Typer Best Practices | `devforge` CLI + `pyproject.toml` entry_points |
| **Configuration as Code** | 12-Factor App / Pydantic Settings | `ConfigRegistry` 패턴, 파일 분리 유지 |
| **Container-First** | Cloud Native Buildpacks / Podman | 멀티스테이지 Dockerfile, 단일 이미지 다중 진입점 |
| **Observability** | OpenTelemetry / Structured Logging | JSON 로깅, 메트릭 엔드포인트 |

### 3.2 목표 디렉토리 구조
```
/opt/projects/server/
├── pyproject.toml              # 단일 설정 (build, deps, tools, entry_points)
├── Dockerfile                  # 멀티스테이지 빌드
├── README.md
├── LICENSE
├── src/
│   └── devforge/               # 메인 패키지 (설치 가능)
│       ├── __init__.py
│       ├── cli.py              # Typer 메인 진입점
│       ├── main.py             # 대안 진입점
│       │
│       ├── core/               # 공통 인프라 (도메인 독립적)
│       │   ├── __init__.py
│       │   ├── config.py       # ConfigRegistry (Pydantic Settings)
│       │   ├── database.py     # SQLAlchemy 2.0 async pool
│       │   ├── logging.py      # JSON 구조화 로깅
│       │   └── exceptions.py
│       │
│       ├── domain/             # 비즈니스 도메인 (Bounded Contexts)
│       │   ├── __init__.py
│       │   ├── turn_collection/    # turn_watcher, parsers
│       │   ├── pipeline/           # extract, enrich, embed, review
│       │   ├── inference/          # pod_manager, model_registry
│       │   ├── storage/            # oci_storage, file_registry
│       │   ├── notification/       # notify, slack, telegram
│       │   ├── watchdog/           # health checks, recovery
│       │   ├── golden_image/       # Azure 이미지 관리
│       │   ├── file_exchange/      # blob_explorer
│       │   └── research/           # exa, context7, web search
│       │
│       ├── interfaces/         # 외부 어댑터 (Ports & Adapters)
│       │   ├── __init__.py
│       │   ├── mcp/            # FastMCP 서버 + Tools
│       │   ├── fastapi/        # REST API 허브
│       │   ├── proxies/        # LLM API 프록시
│       │   └── cli/            # CLI 서브커맨드
│       │       ├── watchdog.py
│       │       ├── mcp.py
│       │       ├── pipeline.py
│       │       └── turn_watcher.py
│       │
│       └── pipelines/          # 오케스트레이션 (day_cycle.sh → Python)
│           ├── __init__.py
│           ├── orchestrator.py # PipelineOrchestrator 클래스
│           ├── day_cycle.py    # 일일 사이클 실행
│           ├── extract.py
│           ├── enrich.py
│           ├── embed.py
│           └── review.py
│
├── tests/                      # 테스트 스위트 (src/ 미러링)
│   ├── unit/
│   ├── integration/
│   ├── fixtures/
│   └── conftest.py
│
├── scripts/                    # 운영/배포 스크립트만 (진입점 X)
│   ├── deploy.sh
│   ├── backup.sh
│   └── migrate.py
│
├── docs/                       # 기존 유지
├── containers/                 # Quadlet/Containerfile
├── config/                     # 설정 템플릿 (.example)
└── .github/workflows/          # CI/CD
```

### 3.3 핵심 인터페이스 계약

```python
# src/devforge/core/config.py — ConfigRegistry 패턴
class ConfigRegistry:
    """단일 진입점 — 내부적으로 파일 5개 물리적 분리 유지"""
    secrets: SecretsConfig      # secrets.env → Pydantic 검증
    runtime: RuntimeConfig      # current-mode-inference.env
    system: SystemConfig        # current-system-mode.env
    persistent: PersistentConfig # state.yaml + CLAUDE.yaml (YAML 직렬화)
    
    @property
    def db_url(self) -> str: ...
    @property
    def inference_mode(self) -> str: ...

# src/devforge/domain/inference/container.py — 추상화된 컨테이너 관리
class InferenceContainerManager(ABC):
    @abstractmethod
    async def ensure_model(self, model_key: str, mode: str) -> bool: ...
    @abstractmethod
    async def exec_secondary(self, model_key: str) -> bool: ...
    @abstractmethod
    async def check_port_forwarding(self) -> tuple[bool, str]: ...

# src/devforge/pipelines/orchestrator.py — day_cycle.sh 로직 클래스화
class PipelineOrchestrator:
    def __init__(self, budget_seconds: int = 21600):
        self.budget = BudgetManager(budget_seconds)
        self.inference = InferenceContainerManager()
    
    async def run_full_cycle(self, mode: str = "full") -> CycleResult: ...
    async def run_incremental(self, from_phase: str) -> CycleResult: ...
    def get_status(self) -> PipelineStatus: ...
```

---

## 4. 단계별 상세 실행 계획

### Phase 0: 기반 구조 구축 (Week 1)

| 작업 | 산출물 | 검증 기준 | 롤백 계획 |
|------|--------|-----------|-----------|
| `pyproject.toml` 작성 (deps, entry_points, tools) | 루트 `pyproject.toml` | `pip install -e .` 성공 | 브랜치 유지 |
| `src/devforge/` 골격 디렉토리 생성 | 20+ `__init__.py` | `import devforge` 성공 | 디렉토리 삭제 |
| `ConfigRegistry` 구현 | `core/config.py` | 기존 5개 파일 파싱 성공 | 기존 `lib.db` 병행 사용 |
| `DatabaseGateway` 인터페이스 정의 | `core/database.py` | `asyncpg` 풀 생성 성공 | 기존 `psql()` 래퍼 유지 |
| 로깅 표준화 (`structlog` + JSON) | `core/logging.py` | 컨테이너 stdout JSON 출력 | 기존 `print` 병행 |

**진입 조건**: 기존 시스템 정상 가동 중  
**완료 조건**: `devforge --help` 동작, 기존 `cli.py status --json`과 출력 비교 검증

### Phase 1: 추론 컨테이너 관리 도메인화 (Week 2)

| 작업 | 산출물 | 검증 기준 |
|------|--------|-----------|
| `InferenceContainerManager` 인터페이스 + `SubprocessImpl` 구현 | `domain/inference/container.py` | 컨테이너 기동/정지/헬스체크 성공 |
| `ModelRegistry` 이동 + 타입힌트 강화 | `domain/inference/models.py` | `MODEL_METADATA` 조회 성공 |
| `podman-py` 선택적 도입 (라이프사이클만) | `domain/inference/podman_client.py` | Rootless 포트 포워딩 제외 검증 |
| `cli.py` 내 `_switch_mode` 등 이관 | `interfaces/cli/inference.py` | `devforge inference switch day` 동작 |

**리스크 완화**: `podman exec` 세컨더리 서버, `systemctl`은 `subprocess` 유지

### Phase 2: Watchdog 도메인화 (Week 3)

| 작업 | 산출물 | 검증 기준 |
|------|--------|-----------|
| `WatchdogOrchestrator` 클래스화 (상태 머신 분리) | `domain/watchdog/orchestrator.py` | 60초 루프 정상 동작 |
| `ComponentTracker` + `GraduatedRecovery` 별도 모듈 | `domain/watchdog/recovery.py` | 백오프/서킷브레이커 보존 |
| `IssueCollector` 연동 (Phase 4 선행) | `domain/watchdog/issue_collector.py` | 자동 수집 트리거 동작 |
| MCP 도구 `watchdog_*` 분리 | `interfaces/mcp/tools/watchdog/` | `devforge watchdog status` 동작 |
| Systemd quadlet 파일 수정 (이미지 경로만) | `containers/*.container` | `systemctl --user restart` 무중단 |

**핵심**: 기존 `watchdog.py`를 `devforge-watchdog` 심볼릭 링크로 래핑, 점진적 전환

### Phase 3: 파이프라인 도메인화 + 오케스트레이터 (Week 4-5)

| 주차 | 작업 | 산출물 |
|------|------|--------|
| **Week 4** | 파이프라인 단계별 모듈화 (`extract/`, `enrich/`, `embed/`, `review/`) | `domain/pipeline/` 패키지 |
| | `PipelineOrchestrator` 구현 (예산 관리, 배치 예약, 상태 머신) | `pipelines/orchestrator.py` |
| | `day_cycle.sh` 로직 Python 클래스로 완전 이관 | `pipelines/day_cycle.py` |
| **Week 5** | MCP 도구 `pipeline_*` 7개 추가 | `interfaces/mcp/tools/pipeline/` |
| | 오케스트레이션 MCP 도구 4개 (`orchestrate`, `status`, `resume`, `budget_gate`) | `interfaces/mcp/tools/orchestration/` |
| | `day_cycle.sh`를 `devforge pipeline orchestrate` 래퍼로 축소 | `scripts/day_cycle.sh` (10줄) |

**검증**: 구/신 `day_cycle` 병렬 실행 1주일, DB 상태(`pipeline_state` 분포, 팩트 수) 비교

### Phase 4: Turn Collection + MCP 도구 재구성 (Week 6)

| 작업 | 산출물 | 검증 기준 |
|------|--------|-----------|
| `TurnWatcher` 클래스화 + 파서 모듈화 | `domain/turn_collection/` | 3초 폴링, 체크포인트 병합 로직 보존 |
| MCP 도구 도메인별 분리 (18개 → 8개 네임스페이스) | `interfaces/mcp/tools/{knowledge,pipeline,inference,actions,watchdog,deepdive}/` | `devforge mcp tools`로 계층적 탐색 가능 |
| `AgentInterface` 추상화 (Web/CLI/Scheduled) | `interfaces/agents/base.py` | 공통 인터페이스 준수 확인 |
| 배치 리뷰 시스템 (`collected_issues` + MCP/CLI) | `domain/watchdog/issue_collector.py` + tools | P0/P1 이슈 자동 수집 → 주간 리포트 생성 |

### Phase 5: 잔여 도메인 + 인터페이스 완성 (Week 7)

| 도메인 | 작업 | 비고 |
|--------|------|------|
| `storage/` | `oci_storage`, `file_registry`, `backup` 이관 | OCI SDK 직접 사용 |
| `notification/` | `notify`, `slack`, `telegram`, `apprise` 통합 | `Apprise` 단일 래퍼 |
| `file_exchange/` | `blob_explorer` 패키지화 | FastAPI 라우터 분리 |
| `research/` | `exa`, `context7`, `web` 통합 | `ResearchFacade` 유지 |
| `golden_image/` | 심볼릭 링크 제거, 로컬 복사 | `_archive/` 이동 후 재배치 |

### Phase 6: 컨테이너화 + CI/CD + 문서 (Week 8)

| 작업 | 산출물 |
|------|--------|
| 멀티스테이지 `Dockerfile` (builder → runtime) | 단일 이미지, 다중 진입점 |
| Quadlet 파일 수정 (`Image=localhost/devforge:latest`) | 기존 systemd 서비스 무중단 전환 |
| GitHub Actions CI (ruff, mypy, pytest, build) | PR마다 자동 검증 |
| `docs/ARCHITECTURE.md` 업데이트 (새 구조 반영) | 문서 동기화 |
| 마이그레이션 가이드 작성 (`docs/MIGRATION_GUIDE.md`) | 팀 온보딩용 |

---

## 5. 업계 표준 비교 분석

### 5.1 프로젝트 구조 비교

| 측면 | 현재 (As-Is) | 목표 (To-Be) | 업계 표준 (참조) |
|------|--------------|--------------|------------------|
| **패키지 레이아웃** | Flat `scripts/` | `src/devforge/` (src-layout) | [Python Packaging Guide](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) — "src layout 권장" |
| **진입점** | 5개 루트 파일 | `pyproject.toml` `[project.scripts]` | [FastAPI Bigger Applications](https://fastapi.tiangelo.com/tutorial/bigger-applications/) — 단일 `main.py` + 라우터 |
| **설정 관리** | 5개 분산 파일 | `ConfigRegistry` (Pydantic Settings) | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — `BaseSettings` + 다중 소스 |
| **도메인 분리** | `lib/` 28개 섞임 | Bounded Contexts (`domain/`) | [DDD / eShopOnContainers](https://github.com/dotnet-architecture/eShopOnContainers) — 마이크로서비스 단위 |
| **아키텍처 패턴** | 계층 혼재 | Ports & Adapters (Hexagonal) | [Cockburn Hexagonal](https://alistair.cockburn.us/hexagonal-architecture/) — 핵심/어댑터 분리 |
| **컨테이너 빌드** | 단일 Dockerfile, 스크립트 복사 | 멀티스테이지, 휠 설치 | [Python Docker Best Practices](https://docs.docker.com/language/python/build-images/) — 빌드/런타임 분리 |
| **프로세스 관리** | systemd + shell + subprocess | 단일 이미지, 다중 진입점 | [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) — systemd 통합 |

### 5.2 AI Agent 시스템 아키텍처 비교

| 측면 | 현재 | 목표 | 업계 표준 (LangGraph / Pydantic AI) |
|------|------|------|-------------------------------------|
| **상태 관리** | DB `pipeline_state` + 파일 체크포인트 | 영속적 상태 머신 + 체크포인트 | [LangGraph Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) — 자동 재개 |
| **Human-in-the-loop** | Slack 버튼 (NEUTRAL/Noise) | 인터럽트 기반 일시정지/재개 | [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) |
| **메모리** | `turns` + `review_facts` + `embeddings` | 단기/장기 메모리 분리 | [LangGraph Memory](https://docs.langchain.com/oss/python/langgraph/memory) — 작동/장기 메모리 |
| **에이전트 타입** | 단일 MCP 서버 | Web/CLI/Scheduled 에이전트 분리 | [Pydantic AI Agents](https://pydantic.dev/pydantic-ai/) — 타입세이프 에이전트 |
| **툴 레지스트리** | 플랫 18개 도구 | 네임스페이스 계층 (knowledge/pipeline/...) | MCP Spec 2025-03-26 — 도구 그룹화 |

### 5.3 설정 관리 패턴 비교

| 패턴 | 현재 | 목표 | Pydantic Settings 권장 |
|------|------|------|------------------------|
| **시크릿** | `secrets.env` (평문) | `SecretsConfig` (검증 + 타입) | `BaseSettings` + `env_file` + `extra='ignore'` |
| **런타임 설정** | `current-mode-inference.env` | `RuntimeConfig` (자동 리로드) | `SettingsConfigDict(env_file=..., validate_default=True)` |
| **영구 상태** | `state.yaml` + `CLAUDE.yaml` | `PersistentConfig` (YAML 직렬화) | `BaseModel` 기반 커스텀 소스 |
| **우선순위** | 불명확 (마지막 로드 승) | 명시적: env > dotenv > yaml > default | `settings_customise_sources()`로 제어 |

---

## 6. 리스크 평가 및 완화 전략

| 리스크 | 발생 가능성 | 영향도 | 완화 전략 |
|--------|-------------|--------|-----------|
| **기존 시스템 중단** | Medium | Critical | Phase별 병렬 실행, feature flag로 구/신 동시 운영 |
| **데이터 손실 (pipeline_state)** | Low | Critical | DB 마이그레이션 스크립트 + 롤백 포인트, 트랜잭션 래퍼 |
| **Podman-py Rootless 한계** | High | Medium | 컨테이너 라이프사이클만 사용, `exec`/`port-forward`는 subprocess 유지 |
| **설정 마이그레이션 누락** | Medium | High | `ConfigRegistry` 단위 테스트로 5개 파일 100% 커버 |
| **day_cycle.sh 로직 누락** | Medium | High | 오케스트레이터 단위 테스트 + 1주일 병렬 실행 비교 |
| **MCP 도구 인터페이스 변경** | Low | Medium | 기존 도구명 별칭 유지 (`devforge-mcp` → `devforge mcp`) |
| **팀 학습 곡선** | High | Medium | 문서화 + 페어 프로그래밍 + 점진적 전환 |

---

## 7. 성공 지표 (KPI)

| 지표 | 현재 | 목표 | 측정 방법 |
|------|------|------|-----------|
| **AI 에이전트 진입점 탐색 시간** | >5분 (파일 50개 중 추측) | <10초 (`devforge --help`) | 사용성 테스트 |
| **LSP 심볼 해결 성공률** | ~60% (순환 참조로 실패) | >95% | `ruff check` + `pyright` 통과율 |
| **컨테이너 빌드 시간** | ~3분 (스크립트 복사) | <1분 (휠 캐시) | CI 로그 |
| **신규 개발자 온보딩** | 2주 (구조 파악) | 2일 (`devforge --help` + 문서) | 설문조사 |
| **배포 롤백 시간** | 30분 (수동) | 5분 (`podman rollback`) | 사고 대응 훈련 |
| **테스트 커버리지** | 0% (통합 테스트만) | >80% (단위 + 통합) | `pytest --cov` |
| **타입 힌트 커버리지** | ~20% | >90% | `mypy --strict` |

---

## 8. 문서화 계획

| 문서 | 위치 | 작성 시점 | 담당 |
|------|------|-----------|------|
| `REFACTORING_PLAN.md` (본 문서) | `docs/` | Phase 0 시작 전 | Lead |
| `ARCHITECTURE.md` (새 구조) | `docs/` | Phase 1 완료 후 | Architect |
| `MIGRATION_GUIDE.md` | `docs/` | Phase 3 완료 후 | Team |
| `API_REFERENCE.md` (MCP Tools) | `docs/` | Phase 4 완료 후 | Backend |
| `OPERATIONS_GUIDE.md` (배포/운영) | `docs/` | Phase 6 완료 후 | DevOps |
| `ADR/` (Architecture Decision Records) | `docs/adr/` | 각 Phase 결정 시 | All |

---

## 9. 부록: 용어 정의

| 용어 | 정의 |
|------|------|
| **Bounded Context** | DDD에서 도메인 모델이 적용되는 명확한 경계. 독립적으로 배포/진화 가능 |
| **Ports & Adapters** | 핵심 비즈니스 로직(Port)과 외부 기술(Adapter)을 인터페이스로 분리 |
| **src-layout** | 패키지 소스를 `src/<package>/` 하위에 두어 `pip install -e .`로 개발 설치 가능하게 하는 구조 |
| **Graduated Recovery** | 실패 횟수에 따라 백오프 증가 → 서킷 브레이커 → 알림으로 단계적 복구 |
| **Pipeline State Machine** | `raw → pending → batching → cleaned → scanned → verified → enriched → embedded` 상태 전이 |
| **ConfigRegistry** | 다중 설정 파일을 단일 객체로 통합 접근하게 하는 레지스트리 패턴 |

---

## 10. 승인

| 역할 | 이름 | 서명 | 날짜 |
|------|------|------|------|
| Technical Lead | | | |
| DevOps Lead | | | |
| Team Members | | | |

---

> **참고**: 이 문서는 살아있는 문서입니다. 각 Phase 완료 시 실제 구현 내용에 맞춰 업데이트하며, `docs/adr/`에 주요 결정 사항을 별도 기록합니다.