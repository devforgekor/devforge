# DevForge 서버 리팩토링 종합 계획서

> **작성일**: 2026-09-13  
> **버전**: 1.1  
> **작성자**: DevForge Team  
> **상태**: Final Draft — 3자 리뷰 반영 (DeepSeek, Claude #1, Claude #2)  
> **Changelog**: v1.0 → v1.1 — [아키텍처 정렬, 12주 일정, 통합 테스트, 하드코딩 경로 해결, 모델 공급자 분리]

---

## 0. 리뷰 반영 요약

| 리뷰어 | 등급 | 핵심 지적 | 대응 |
|--------|------|-----------|------|
| **DeepSeek** | B− | 일정 낙관적, 테스트 빈약, podman-py 리스크, ConfigRegistry 마이그레이션 세부항목 부족 | 8주→12주, Phase 0.5 특성화 테스트 추가 |
| **Claude #1** | B− | 포장은 좋고 실행은 아프다고 평가 | Phase −1(정리) 추가, 하드코딩 40곳 Phase 0에 포함 |
| **Claude #2** (구조 분석) | Crit | architecture 위반: domain에 adapter, 이름 충돌(pipelines/domain/pipeline), 테스트 공수 0 | 구조 재정의, 12주 재산정 |
| **통합** | — | podman-py 도입 안 함, 하드코딩 경로 해결 필수, 특성화 테스트 선작성 | 반영 |

---

## 1. 개요

### 1.1 목적
현재 `scripts/` 루트에 평평하게 배치된 50+ 진입점 스크립트와 28개 서브모듈로 구성된 `scripts/lib/`을 **업계 표준 Python 패키지 구조(src-layout + Domain-Driven Design + Ports & Adapters)**로 재구성하여, AI 에이전트와 사람이 모두 탐색하기 쉬운 코드베이스를 만든다.

### 1.2 배경
- **현재 문제**: 진입점 분산(`cli.py`, `watchdog.py`, `turn_watcher.py`, `mcp_server.py`, `day_cycle.sh` 등), 순환 참조 위험, 하드코딩된 경로(40+ 곳), 설정 파일 5개 분산, Shell/Python 혼재, 모델 관리 분산(로컬 포트 + 클라우드 공급자 미지원)
- **목표 구조**: 설치 가능한 패키지(`pip install -e .`), 단일 진입점(`devforge` CLI), 도메인별 경계 명확화, 컨테이너 친화적 빌드/실행, **클라우드 LLM 공급자(OpenAI/Anthropic) 지원**

### 1.3 범위
- **대상**: `/opt/projects/server/scripts/` (코드), `/opt/projects/server/docs/` (문서 구조 유지)
- **제외**: `_archive/` 과거 산출물, `/opt/workspace/` 외부 워크스페이스 (golden_image 심볼릭 링크는 Phase 0에 정리)
- **일정**: **12주 (Phase −1 ~ Phase 7)**, 버퍼 및 검증 기간 포함

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
│   ├── day_cycle.sh (19KB)     # 셸 오케스트레이터 (455줄)
│   ├── *.bak, *.backup, *.old  # 중복/백업 10개+
│   ├── golden_image -> ...     # 심볼릭 링크 (도구 깨짐)
│   └── lib/ (28개 서브디렉토리) # "유틸리티 창고", 도메인 경계 불명확
├── docs/                       # 문서 (유지)
├── containers/                 # 컨테이너 정의 (Quadlet)
├── config/                     # 설정 템플릿
└── (pyproject.toml 없음)        # 설정 분산 (ruff.toml, pytest.ini 등)
```

### 2.2 핵심 컴포넌트 런타임 동작

| 컴포넌트 | 현재 위치 | 진입점 | 상태 관리 | 의존성 |
|----------|-----------|--------|-----------|--------|
| **Turn Collection** | `turn_watcher.py` + `lib/parsers/` | systemd service | `collect_checkpoint.json` | DB, 파일시스템 |
| **Pipeline (day_cycle)** | `day_cycle.sh` + `pipelines/*.py` | systemd timer | `pipeline_state` (DB) | Inference, DB |
| **Inference Mgmt** | `lib/pod_manager/` | `cli.py` 내부 함수 | `current-mode-inference.env` | Podman, 로컬 모델 파일 |
| **Watchdog** | `watchdog.py` → `lib/watchdog/orchestrator.py` | systemd service | 메모리 + `watchdog_liveness` | systemd, Podman, Slack |
| **MCP Server** | `mcp_server.py` | systemd service | DB (stateless) | Inference(:8080-8084), DB |
| **LLM Client** | `lib/llm_client/__init__.py` | import by pipelines | `MODEL_REGISTRY` (하드코딩 포트) | 로컬 포트(8080-8084) |
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

LLM Client (lib/llm_client/__init__.py):
    MODEL_REGISTRY = {port 8080-8084 hardcoded} → http://127.0.0.1:{port}/v1/chat/completions
```

### 2.4 주요 기술적 부채

| 구분 | 내용 | 영향도 |
|------|------|--------|
| **진입점 분산** | 5개 메인 진입점이 루트에 동급 배치 | AI 에이전트 탐색 실패, 시작 지점 불명확 |
| **설정 분산** | `state.yaml`, `CLAUDE.yaml`, `secrets.env`, `current-mode-inference.env`, `current-system-mode.env` | 환경별 설정 관리 난이도 ↑ |
| **Shell/Python 혼재** | `day_cycle.sh` 455줄 + Python 파이프라인 | 테스트/디버깅/타입힌트 불가 |
| **하드코딩 경로** | `/opt/ai_data/...`, `/opt/projects/server/...` 40+ 곳 | 컨테이너 이식성 없음 (Phase 0 반드시 해결) |
| **순환 참조 위험** | `cli.py` → `lib.*` → `scripts.*` 양방향 | LSP 심볼 해결 실패 (95% 목표 불가) |
| **중복 파일** | `*.bak`, `*.backup`, `*.old` 10개+ | 에이전트 혼란 |
| **심볼릭 링크** | `golden_image → /opt/workspace/...` | Git/LSP/도구 체인 깨짐 |
| **모델 공급자 분산** | 로컬 포트 하드코딩, 클라우드 API 지원 없음 | 유연성 저하, 키 관리 불가 |
| **podman-py 비도입 결정** | rootless 포트 포워딩 제어 불가 | subprocess 유지 (추후 재평가) |

---

## 3. 목표 아키텍처 (To-Be)

### 3.1 설계 원칙 (업계 표준 준수)

| 원칙 | 참조 표준 | 적용 방식 |
|------|-----------|-----------|
| **src-layout** | Python Packaging Authority | `src/devforge/` 패키지, `pip install -e .` |
| **Domain-Driven Design** | Evans DDD / Microsoft eShopOnContainers | Bounded Contexts = `domain/` 서브패키지 |
| **Ports & Adapters (Hexagonal)** | Alistair Cockburn | `ports/` = Protocol, `adapters/` = 구현체, `domain/` = Core 순수 로직 |
| **Single Entry Point** | Typer Best Practices | `devforge` CLI + `pyproject.toml` entry_points (main.py 제거) |
| **Configuration as Code** | 12-Factor App / Pydantic Settings | `ConfigRegistry` 패턴, 파일 물리 분리 유지 |
| **Container-First** | Cloud Native Buildpacks / Podman | 멀티스테이지 Dockerfile, Quadlet |
| **Observability** | OpenTelemetry / Structured Logging | JSON 로깅, 메트릭 엔드포인트 |
| **Test-First Migration** | Michael Feathers | 특성화 테스트 → 리팼토링 → 회귀 테스트 |

### 3.2 목표 디렉토리 구조 (수정본)

```
/opt/projects/server/
├── pyproject.toml              # 단일 설정 (build, deps, tools, entry_points)
├── Dockerfile                  # 멀티스테이지 빌드
├── README.md
├── LICENSE
├── src/
│   └── devforge/               # 메인 패키지 (설치 가능)
│       ├── __init__.py
│       ├── cli.py              # Typer 메인 진입점 (단일)
│       │
│       ├── core/               # 공통 인프라 (도메인 독립적)
│       │   ├── __init__.py
│       │   ├── config.py       # ConfigRegistry (Pydantic Settings)
│       │   ├── database.py     # SQLAlchemy 2.0 async pool
│       │   ├── logging.py      # JSON 구조화 로깅
│       │   └── exceptions.py
│       │
│       ├── ports/              # 인터페이스 정의 (Protocols)
│       │   ├── __init__.py
│       │   └── inference.py    # LLMProvider Protocol
│       │
│       ├── domain/             # 비즈니스 도메인 (Bounded Contexts, 순수 로직)
│       │   ├── __init__.py
│       │   ├── turn_collection/    # parsers, watcher, collector
│       │   ├── pipeline/           # extract, enrich, embed, review 로직
│       │   ├── watchdog/           # health checks, recovery, issue collector
│       │   └── golden_image/       # Azure 이미지 메타데이터
│       │
│       ├── adapters/           # 외부 기술 구현체 (driven + driving)
│       │   ├── __init__.py
│       │   ├── driven/
│       │   │   ├── __init__.py
│       │   │   ├── llm/              # LLM 공급자 어댑터
│       │   │   │   ├── local.py      # 로컬 포트 기반 (8080-8084)
│       │   │   │   ├── openai.py     # OpenAI API (ChatGPT)
│       │   │   │   ├── anthropic.py  # Anthropic API (Claude)
│       │   │   │   └── registry.py   # 공급자 팩토리 + 라우팅
│       │   │   ├── storage/          # OCI Object Storage, FileRegistry
│       │   │   ├── container/        # Podman subprocess 구현체
│       │   │   ├── notification/     # Slack, Telegram, Apprise
│       │   │   └── research/         # exa, context7, web search
│       │   └── driving/
│       │       ├── __init__.py
│       │       ├── mcp/            # FastMCP 서버 + Tools
│       │       ├── fastapi/        # REST API 허브
│       │       ├── cli/            # CLI 서브커맨드 (watchdog, mcp, pipeline, turn_watcher)
│       │       └── proxies/        # LLM API 프록시
│       │
│       ├── application/        # 애플리케이션 서비스 / 오케스트레이션
│       │   ├── __init__.py
│       │   ├── orchestrator.py # PipelineOrchestrator (day_cycle.sh → Python)
│       │   ├── day_cycle.py    # 일일 사이클 실행
│       │   ├── agent_interface.py # Web/CLI/Scheduled 에이전트 추상화
│       │   └── issue_collector.py  # 배치 리뷰 시스템
│       │
│       └── pipelines/          # 단계별 모듈 (도메인 로직 호출)
│           ├── __init__.py
│           ├── extract.py
│           ├── enrich.py
│           ├── embed.py
│           └── review.py
│
├── tests/                      # 테스트 스위트 (src/ 미러링)
│   ├── unit/
│   ├── integration/
│   ├── characterization/       # Phase 0.5: 기존 동작 보호
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
    """단일 진입점 — 물리적 파일 5개 분리 유지, 우선순위: env > dotenv > yaml > default"""
    secrets: SecretsConfig      # secrets.env → Pydantic 검증
    model_providers: ModelProvidersConfig  # providers.yaml (OpenAI, Anthropic, Local)
    runtime: RuntimeConfig      # current-mode-inference.env
    system: SystemConfig        # current-system-mode.env
    persistent: PersistentConfig # state.yaml + CLAUDE.yaml (YAML 직렬화)
    
    @property
    def db_url(self) -> str: ...
    @property
    def inference_mode(self) -> str: ...

# src/devforge/ports/inference.py — LLM 공급자 포트 (Protocol)
class LLMProvider(Protocol):
    provider_name: str
    api_key: str
    base_url: str
    
    async def chat(self, messages: list[dict], **kwargs) -> LLMResult: ...
    async def embeddings(self, texts: list[str]) -> list[list[float]]: ...

# src/devforge/adapters/driven/llm/registry.py — 공급자 구현 및 라우팅
class LLMProviderFactory:
    """OpenAI, Anthropic, Local 포트 기반 모델을 라우팅. 동일 키로 다중 공급자 지원."""
    @staticmethod
    def create(provider_type: str, api_key: str, **kwargs) -> LLMProvider: ...

# src/devforge/adapters/driven/llm/local.py — 로컬 포트 기반 (현재 방식)
class LocalLLMProvider:
    def __init__(self, port: int, model_name: str): ...
    async def chat(self, messages, **kwargs) -> LLMResult:
        # http://127.0.0.1:{port}/v1/chat/completions
        ...

# src/devforge/adapters/driven/llm/openai.py — OpenAI API (ChatGPT)
class OpenAILLMProvider:
    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1"): ...
    async def chat(self, messages, **kwargs) -> LLMResult:
        # OpenAI 호환 API 호출
        ...

# src/devforge/adapters/driven/llm/anthropic.py — Anthropic API (Claude)
class AnthropicLLMProvider:
    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com"): ...
    async def chat(self, messages, **kwargs) -> LLMResult:
        # Anthropic API 호출 (Messages API)
        ...

# src/devforge/application/orchestrator.py — day_cycle.sh 로직 클래스화
class PipelineOrchestrator:
    def __init__(self, budget_seconds: int = 21600):
        self.budget = BudgetManager(budget_seconds)
        self.llm = LLMProviderFactory()  # 공급자 기반 라우팅
    
    async def run_full_cycle(self, mode: str = "full") -> CycleResult: ...
    async def run_incremental(self, from_phase: str) -> CycleResult: ...
    def get_status(self) -> PipelineStatus: ...
```

### 3.4 LLM 공급자 설정 (신규)

```yaml
# config/providers.yaml.example
providers:
  openai:
    type: "openai"
    api_key_env: "OPENAI_API_KEY"        # secrets.env에서 주입
    base_url: "https://api.openai.com/v1"
    default_models:
      chat: "gpt-4.1-mini"
      embed: "text-embedding-3-small"
  
  anthropic:
    type: "anthropic"
    api_key_env: "ANTHROPIC_API_KEY"     # 같은 키 사용 가능 (환경별 매핑)
    base_url: "https://api.anthropic.com"
    default_models:
      chat: "claude-3-5-sonnet-20241022"
      reasoning: "claude-3-7-sonnet-20250219"
  
  local:
    type: "local"
    ports:
      embeder: 8081
      reranker: 8080
      extractor: 8082
      enricher: 8082
      verifier: 8084
      judge: 8083
```

```python
# secrets.env (업데이트)
OPENAI_API_KEY=your-key-here
ANTHROPIC_API_KEY=your-key-here  # 동일 키 사용 가능 (OpenAI/Anthropic 모델 라우팅 시)
```

---

## 4. 단계별 상세 실행 계획 (12주)

### Phase −1: 정리 및 기저장치 캡처 (Week 0.5, 2일)

| 작업 | 산출물 | 검증 기준 |
|------|--------|-----------|
| 고장난 심볼릭 링크 정리 (`golden_image → /opt/workspace/`) | 로컬 복사본 | `ls -la scripts/golden_image` → 파일 존재 |
| `*.bak`, `*.backup`, `*.old` 파일 10개+ 정리 | `_archive/` 이동 | `find . -name "*.bak"` → 0개 |
| 하드코딩 경로 인벤토리 작성 | `data/hardcoded_paths.csv` | 40+ 경로 목록 |
| `day_cycle.sh` 455줄 행위 명세화 | `data/day_cycle_behavior.md` | 주석 단위 동작 문서화 |
| 골든마스터 캡처 (DB + 상태 + 이미지) | `data/golden_master.json` | 상태 스냅샷 보증 |

**완료 조건**: `pip install -e .` 전 단계, 현재 시스템 완벽 복제본 확보

### Phase 0: 기반 구축 + 특성화 테스트 (Week 1-2)

| 주차 | 작업 | 산출물 | 검증 기준 |
|------|------|--------|-----------|
| **Week 1** | `pyproject.toml` 작성 (deps, entry_points, tools) | 루트 `pyproject.toml` | `pip install -e .` 성공 |
| | `src/devforge/` 골격 디렉토리 생성 (v1.1 구조) | 30+ `__init__.py` | `import devforge` 성공 |
| | `ConfigRegistry` 구현 (`providers.yaml` 포함) | `core/config.py` | 기존 5개 파일 파싱 성공 |
| | `DatabaseGateway` 인터페이스 정의 | `core/database.py` | `asyncpg` 풀 생성 성공 |
| | 로깅 표준화 (`structlog` + JSON) | `core/logging.py` | 컨테이너 stdout JSON 출력 |
| | `import-linter` 설정 (순환 참조 방지) | `pyproject.toml` | `linter` 0 violations |
| | 하드코딩 경로 40곳 추상화 (`Paths`) | `core/paths.py` | `Paths.data_dir == "/opt/ai_data"` 검증 |
| **Week 2** | **특성화 테스트 작성 (5개 핵심 경로)** | `tests/characterization/` | 기존 동작 보존 (fixture 기반) |
| | `ModelProvider` 포트 정의 + `Local` 구현 | `ports/inference.py` + `adapters/driven/llm/local.py` | 로컬 포트 8080-8084 호출 성공 |
| | `OpenAILLMProvider` + `AnthropicLLMProvider` 구현 | `adapters/driven/llm/openai.py`, `anthropic.py` | API 호출 테스트 (목업) |
| | `LLMProviderFactory` 구현 | `adapters/driven/llm/registry.py` | provider_type별 라우팅 |
| | `Alembic` DB 마이그레이션 설정 | `alembic/` | `alembic upgrade head` 성공 |

**진입 조건**: Phase −1 완료, 골든마스터 확보  
**완료 조건**: `devforge --help` 동작, 기존 `cli.py status --json`과 출력 비교, 특성화 테스트 100% 통과

### Phase 1: LLM 공급자 + 추론 컨테이너 도메인화 (Week 3-4)

| 주차 | 작업 | 산출물 | 검증 기준 |
|------|------|--------|-----------|
| **Week 3** | `LLMProvider` 포트 + 3 구현체 (Local/OpenAI/Anthropic) 완료 | `ports/inference.py` + 3개 어댑터 | 로컬/클라우드 라우팅 테스트 |
| | `ModelRegistry` 도메인화 (공급자별 메타데이터) | `domain/model_registry.py` | `get_model(provider, "gpt-4o-mini")` 성공 |
| | `InferenceContainerManager` 인터페이스 + `SubprocessImpl` | `domain/inference/container.py` | 컨테이너 기동/정지/헬스체크 성공 |
| | `cli.py` 내 inference 서브커맨드 이전 | `interfaces/cli/inference.py` | `devforge inference list` 동작 |
| | API 키 관리 (secrets.env → ConfigRegistry) | `core/config.py` | `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` 주입 |
| **Week 4** | `podman-py` 비도입 확정 검토 | - | subprocess 방식 유지 결정 |
| | `systemctl` 연동 (subprocess 유지) | - | systemd 서비스 무중단 재시작 |
| | 특성화 테스트와 신규 테스트 병행 | `tests/` | 컨테이너 매니저 테스트 80%+ |

**핵심**: `podman exec`, `systemctl --user`은 **subprocess 유지**. podman-py는 rootless 포트 포워딩 제어 불가로 비도입 확정.

### Phase 2: Watchdog 도메인화 + IssueCollector (Week 5)

| 작업 | 산출물 | 검증 기준 |
|------|--------|-----------|
| `WatchdogOrchestrator` 클래스화 (상태 머신 분리) | `domain/watchdog/orchestrator.py` | 60초 루프 정상 동작 |
| `ComponentTracker` + `GraduatedRecovery` 별도 모듈 | `domain/watchdog/recovery.py` | 백오프/서킷브레이커 보존 |
| `IssueCollector` 구현 (수집 → DB 테이블) | `application/issue_collector.py` + `collected_issues` 테이블 | 자동 수집 트리거 동작 |
| MCP 도구 `watchdog_*` 분리 | `interfaces/mcp/tools/watchdog/` | `devforge watchdog status` 동작 |
| Systemd quadlet 파일 수정 (이미지 경로만) | `containers/*.container` | `systemctl --user restart` 무중단 |
| 배치 리뷰 MCP 도구 (`issues batch`) | `interfaces/mcp/tools/admin/` | `devforge mcp issues batch` 동작 |

**핵심**: 기존 `watchdog.py`를 `devforge-watchdog` 심볼릭 링크로 래핑, 점진적 전환

### Phase 3: 파이프라인 도메인화 + 오케스트레이터 (Week 6-7)

| 주차 | 작업 | 산출물 |
|------|------|--------|
| **Week 6** | 파이프라인 단계별 모듈화 (`extract/`, `enrich/`, `embed/`, `review/`) | `domain/pipeline/stages/` |
| | `PipelineOrchestrator` 구현 (예산 관리, 배치 예약, 상태 머신) | `application/orchestrator.py` |
| | `day_cycle.sh` 로직 Python 클래스로 완전 이관 | `application/day_cycle.py` |
| | LLM 공급자 라우팅 적용 (파이프라인 단계별 provider 선택) | `pipelines/*.py` |
| **Week 7** | MCP 도구 `pipeline_*` 7개 추가 | `interfaces/mcp/tools/pipeline/` |
| | 오케스트레이션 MCP 도구 4개 (`orchestrate`, `status`, `resume`, `budget_gate`) | `interfaces/mcp/tools/orchestration/` |
| | `day_cycle.sh`를 `devforge pipeline orchestrate` 래퍼로 축소 (10줄) | `scripts/day_cycle.sh` |
| | **구/신 day_cycle 병렬 실행 시작** | systemd timer 2개 |

**검증**: Week 7-8 구/신 `day_cycle` 병렬 실행 2주, DB 상태(`pipeline_state` 분포, 팩트 수) 비교

### Phase 3.5: 병렬 검증 및 리스크 검증 (Week 8)

| 작업 | 검증 기준 |
|------|-----------|
| 구/신 day_cycle 2주간 DB 상태 대조 | `pipeline_state` 분포, 팩트 수 ±5% |
| LLM 공급자 라우팅 검증 (OpenAI vs Anthropic vs Local) | 응답 시간, 품질 비교 |
| Rollback 테스트 (Quadlet Image= 다이제스트 재고정) | 5분 내 롤백 성공 |

### Phase 4: Turn Collection + MCP 도구 재구성 (Week 9)

| 작업 | 산출물 | 검증 기준 |
|------|--------|-----------|
| `TurnWatcher` 클래스화 + 파서 모듈화 | `domain/turn_collection/` | 3초 폴링, 체크포인트 병합 로직 보존 |
| MCP 도구 도메인별 분리 (18개 → 8개 네임스페이스) | `interfaces/mcp/tools/{knowledge,pipeline,inference,actions,watchdog,deepdive}/` | `devforge mcp tools`로 계층적 탐색 가능 |
| `AgentInterface` 추상화 (Web/CLI/Scheduled) | `application/agent_interface.py` | 공통 인터페이스 준수 확인 |
| 배치 리뷰 시스템 완성 (`collected_issues` + MCP/CLI) | `application/issue_collector.py` + tools | P0/P1 이슈 자동 수집 → 주간 리포트 생성 |

### Phase 5: 잔여 도메인 + 인터페이스 완성 (Week 10)

| 도메인 | 작업 | 비고 |
|--------|------|------|
| `storage/` | `oci_storage`, `file_registry`, `backup` 이전 | `adapters/driven/storage/` |
| `notification/` | `notify`, `slack`, `telegram`, `apprise` 통합 | `adapters/driven/notification/` |
| `file_exchange/` | `blob_explorer` 패키지화 | FastAPI 라우터 분리 |
| `research/` | `exa`, `context7`, `web` 통합 | `adapters/driven/research/` |
| `golden_image/` | 심볼릭 링크 제거 검증 | `_archive/` 이동 후 재배치 완료 |

### Phase 6: 컨테이너화 + CI/CD + 문서 (Week 11)

| 작업 | 산출물 |
|------|--------|
| 멀티스테이지 `Dockerfile` (builder → runtime) | 단일 이미지, 다중 진입점 |
| Quadlet 파일 수정 (`Image=localhost/devforge:latest`) | 기존 systemd 서비스 무중단 전환 |
| GitHub Actions CI (ruff, mypy, pytest, build) | PR마다 자동 검증 |
| `docs/ARCHITECTURE.md` 업데이트 (새 구조 반영) | 문서 동기화 |
| `docs/MIGRATION_GUIDE.md` 작성 | 팀 온보딩용 |
| `docs/LLM_PROVIDERS.md` 작성 (신규) | OpenAI/Anthropic/Local 공급자 설정 가이드 |

### Phase 7: 안정화 및 롤백 (Week 12)

| 작업 | 검증 |
|------|------|
| 기존 `scripts/`에서 임포트 루트 완전히 제거 | `python -c "from lib import cli"` → ImportError |
| `collected_issues` 배치 리뷰 (첫 주간 리포트) | 리포트 생성 테스트 |
| 문서 동기화 (`ARCHITECTURE.md`, `MIGRATION_GUIDE.md`) | 최신 구조 반영 |
| 롤백 훈련 (Quadlet Image= 다이제스트 재고정) | 5분 내 롤백 검증 |
| `scripts/`에서 `day_cycle.sh`만 남기고 정리 | 운영 스크립트만 남김 |

---

## 5. 업계 표준 비교 분석

### 5.1 프로젝트 구조 비교

| 측면 | 현재 (As-Is) | 목표 (To-Be) | 업계 표준 (참조) |
|------|--------------|--------------|------------------|
| **패키지 레이아웃** | Flat `scripts/` | `src/devforge/` (src-layout) | [Python Packaging Guide](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) — "src layout 권장" |
| **진입점** | 5개 루트 파일 | `pyproject.toml` `[project.scripts]` (단일 `devforge`) | [Typer Best Practices](https://typer.tiangolo.com/) — 단일 CLI + 서브커맨드 |
| **설정 관리** | 5개 분산 파일 | `ConfigRegistry` (Pydantic Settings) + `providers.yaml` | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — `BaseSettings` + 다중 소스 |
| **도메인 분리** | `lib/` 28개 섞임 | Bounded Contexts (`domain/`) + `adapters/` 분리 | [DDD / eShopOnContainers](https://github.com/dotnet-architecture/eShopOnContainers) — Core/Adapters 분리 |
| **아키텍처 패턴** | 계층 혼재 | Ports & Adapters (Hexagonal) | [Cockburn Hexagonal](https://alistair.cockburn.us/hexagonal-architecture/) — 핵심/어댑터 분리, `ports/` Protocol |
| **컨테이너 빌드** | 단일 Dockerfile, 스크립트 복사 | 멀티스테이지, 휠 설치 | [Python Docker Best Practices](https://docs.docker.com/language/python/build-images/) — 빌드/런타임 분리 |
| **프로세스 관리** | systemd + shell + subprocess | 단일 이미지, Quadlet | [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) — systemd 통합 |
| **테스트 전략** | 없음 (통합 테스트만) | Characterization + Unit + Integration | [Refactoring (Feathers)](https://martinfowler.com/books/refactoring.html) — 리팩토링 전 반드시 테스트 |
| **LLM 공급자** | 로컬 포트 하드코딩 | Provider 추상화 (Local/OpenAI/Anthropic) | [OpenAI SDK](https://github.com/openai/openai-python), [Anthropic SDK](https://github.com/anthropics/anthropic) |

### 5.2 AI Agent 시스템 아키텍처 비교

| 측면 | 현재 | 목표 | 업계 표준 (LangGraph / Pydantic AI) |
|------|------|------|-------------------------------------|
| **상태 관리** | DB `pipeline_state` + 파일 체크포인트 | 영속적 상태 머신 + 체크포인트 (LangGraph) | [LangGraph Durable Execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) — 자동 재개, 체크포인트 |
| **Human-in-the-loop** | Slack 버튼 (NEUTRAL/Noise) | 인터럽트 기반 일시정지/재개 + 배치 리뷰 | [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts) |
| **메모리** | `turns` + `review_facts` + `embeddings` | 단기/장기 메모리 분리 | [LangGraph Memory](https://docs.langchain.com/oss/python/langgraph/memory) — 작동/장기 메모리 |
| **에이전트 타입** | 단일 MCP 서버 | Web/CLI/Scheduled 에이전트 분리 | [Pydantic AI Agents](https://pydantic.dev/pydantic-ai/) — 타입세이프 에이전트 |
| **툴 레지스트리** | 플랫 18개 도구 | 네임스페이스 계층 (knowledge/pipeline/...) | MCP Spec 2025-03-26 — 도구 그룹화 |
| **LLM 공급자** | Local port only | Local + OpenAI + Anthropic | [LiteLLM](https://www.litellm.ai/) — 다중 공급자 통합 |
| **Model Registry** | 하드코딩 포트 (MODEL_REGISTRY) | Provider 기반 (providers.yaml) | 동일 키로 OpenAI/Anthropic 라우팅 |

### 5.3 설정 관리 패턴 비교

| 패턴 | 현재 | 목표 | Pydantic Settings 권장 |
|------|------|------|------------------------|
| **시크릿** | `secrets.env` (평면, 5개 키) | `SecretsConfig` (검증 + 타입) + `providers.yaml` | `BaseSettings` + `env_file` + `extra='ignore'` |
| **런타임 설정** | `current-mode-inference.env` | `RuntimeConfig` (자동 리로드) | `SettingsConfigDict(env_file=..., validate_default=True)` |
| **LLM 공급자** | 하드코딩 포트 | `ModelProvidersConfig` (OpenAI, Anthropic, Local) | `BaseModel` 기반 커스텀 소스 |
| **영구 상태** | `state.yaml` + `CLAUDE.yaml` | `PersistentConfig` (YAML 직렬화) | `BaseModel` 기반 커스텀 소스 |
| **우선순위** | 불명확 (마지막 로드 승) | 명시적: env > dotenv > yaml > default | `settings_customise_sources()`로 제어 |

### 5.4 LLM 공급자 아키텍처

| 컴포넌트 | 설명 |
|----------|------|
| **LLMProvider (Protocol)** | `ports/inference.py` — `chat()`, `embeddings()` 인터페이스 |
| **LocalLLMProvider** | `adapters/driven/llm/local.py` — 로컬 포트 8080-8084 (현재 방식) |
| **OpenAILLMProvider** | `adapters/driven/llm/openai.py` — OpenAI API (ChatGPT) |
| **AnthropicLLMProvider** | `adapters/driven/llm/anthropic.py` — Anthropic API (Claude) |
| **LLMProviderFactory** | `adapters/driven/llm/registry.py` — provider_type별 라우팅, 동일 키 다중 공급자 지원 |
| **ModelRegistry** | `domain/model_registry.py` — 공급자별 모델 메타데이터 (이름, 포트/엔드포인트, 파라미터) |

**API 키 전략**: OpenAI와 Anthropic은 동일한 API 키 문자열을 사용할 수 있음(환경별 매핑). `secrets.env`의 `OPENAI_API_KEY`와 `ANTHROPIC_API_KEY`가 같은 값을 가질 수 있으며, `providers.yaml`에서 공급자별로 매핑한다.

---

## 6. 리스크 평가 및 완화 전략

| 리스크 | 발생 가능성 | 영향도 | 완화 전략 |
|--------|-------------|--------|-----------|
| **기존 시스템 중단** | Medium | Critical | Phase별 병행 실행, feature flag으로 구/신 동시 운영, Phase −1 골든마스터 확보 |
| **데이터 손실 (pipeline_state)** | Low | Critical | DB 마이그레이션 스크립트 + 롤백 포인트(Alembic), 트랜잭션 래퍼 |
| **하드코딩 경로 미해결** | Medium | High | Phase 0에 `Paths` 추상화 필수 (40곳 인벤토리) |
| **순환 참조 재발** | Medium | High | `import-linter` CI 게이트 (0 violations) |
| **특성화 테스트 누락** | Low | High | Phase 0.5에서 5개 핵심 경로 테스트 작성 (Feiertags 강제) |
| **day_cycle.sh 로직 누락** | Medium | High | 오케스트레이터 단위 테스트 + 2주 병렬 실행 비교 |
| **MCP 도구 인터페이스 변경** | Low | Medium | 기존 도구명 별칭 유지 (`devforge-mcp` → `devforge mcp`) |
| **LLM 공급자 마이그레이션** | Medium | Medium | Local → OpenAI/Anthropic 점진적 전환, 품질 비교 |
| **podman-py 미도입** | High | Low | subprocess 기반 추상화 계층(`SubprocessContainerManager`) 유지 |
| **팀 학습 곡선** | High | Medium | 문서화 + 페어 프로그래밍 + 점진적 전환 |

---

## 7. 성공 지표 (KPI)

| 지표 | 현재 | 목표 | 측정 방법 |
|------|------|------|-----------|
| **AI 에이전트 진입점 탐색 시간** | >5분 (파일 50개 중 추측) | <10초 (`devforge --help`) | 사용성 테스트 (3명 AI 에이전트) |
| **LSP 심볼 해결 성공률** | ~60% (순환 참조) | >95% | `pyright --outputjson` 통계, import-linter 0 violations |
| **컨테이너 빌드 시간** | ~3분 (스크립트 복사) | <1분 (휠 캐시) | CI 로그 |
| **신규 개발자 온보딩** | 2주 (구조 파악) | 2일 (`devforge --help` + 문서) | 설문조사 (최소 1명 내부) |
| **배포 롤백 시간** | 30분 (수동) | 5분 (Quadlet digest 고정) | 사고 대응 훈련 (Phase 7) |
| **테스트 커버리지** | 0% (통합 테스트만) | >80% (unit + integration + characterization) | `pytest --cov` |
| **타입 힌트 커버리지** | ~20% | >90% | `mypy --strict` |
| **LLM 공급자 전환 속도** | 0 (Local only) | <5초 (provider 전환) | provider 라우팅 bench |
| **hardcoded path count** | 40+ | 0 | `grep -r "/opt/" scripts/` → 0 (단, config/paths.py 예외) |

---

## 8. 문서화 계획

| 문서 | 위치 | 작성 시점 | 담당 |
|------|------|-----------|------|
| `REFACTORING_PLAN.md` (본 문서 v1.1) | `docs/` | Phase 0 시작 전 | Lead |
| `ARCHITECTURE.md` (새 구조) | `docs/` | Phase 1 완료 후 | Architect |
| `MIGRATION_GUIDE.md` | `docs/` | Phase 3 완료 후 | Team |
| `LLM_PROVIDERS.md` (신규) | `docs/` | Phase 1 완료 후 | Backend |
| `API_REFERENCE.md` (MCP Tools) | `docs/` | Phase 4 완료 후 | Backend |
| `OPERATIONS_GUIDE.md` (배포/운영) | `docs/` | Phase 6 완료 후 | DevOps |
| `ADR/` (Architecture Decision Records) | `docs/adr/` | 각 Phase 결정 시 | All |

---

## 9. 부록: 용어 정의

| 용어 | 정의 |
|------|------|
| **Bounded Context** | DDD에서 도메인 모델이 적용되는 명확한 경계. 독립적으로 배포/진화 가능 |
| **Ports & Adapters** | 핵심 비즈니스 로직(Port = Protocol)과 외부 기술(Adapter)을 인터페이스로 분리 |
| **src-layout** | 패키지 소스를 `src/<package>/` 하위에 두어 `pip install -e .`로 개발 설치 가능하게 하는 구조 |
| **Graduated Recovery** | 실패 횟수에 따라 백오프 증가 → 서킷 브레이커 → 알림으로 단계적 복구 |
| **Pipeline State Machine** | `raw → pending → batching → cleaned → scanned → verified → enriched → embedded` 상태 전이 |
| **ConfigRegistry** | 다중 설정 파일을 단일 객체로 통합 접근하게 하는 레지스트리 패턴 |
| **LLMProvider** | OpenAI, Anthropic, Local 등 LLM 공급자의 공통 추상화 인터페이스 (Protocol) |
| **특성화 테스트 (Characterization Test)** | 리팩토링 전 기존 동작을 고정하는 테스트 (Feiertags의 Legacy 코드 테스트 전략) |

---

## 10. 승인

| 역할 | 이름 | 서명 | 날짜 |
|------|------|------|------|
| Technical Lead | | | |
| DevOps Lead | | | |
| Team Members | | | |

---

> **참고**: 이 문서는 살아있는 문서입니다. 각 Phase 완료 시 실제 구현 내용에 맞춰 업데이트하며, `docs/adr/`에 주요 결정 사항을 별도 기록합니다.
