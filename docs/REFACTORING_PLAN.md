# DevForge 서버 리팩토링 종합 계획서 v1.3

> **작성일**: 2026-09-13  
> **버전**: 1.3  
> **작성자**: DevForge Team  
> **상태**: Final Draft — v1.2 리뷰(DeepSeek) 반영  
> **Changelog**: v1.2 → v1.3  
>   - Track B(LLM 공급자)를 일정에서 분리 (별도 문서)  
>   - 리스크 표 6→10행 복원 (삭제된 Critical 2건 재추가)  
>   - Phase 5/7 시간 재조정 (5→14주)  
>   - "무중단" 표현 수정  
>   - API 키 테스트 문구 수정  
>   - 팀 규모 명시  
>   - §3.4 계약 오류 수정 (BaseModel → BaseSettings, cloudahq 오류)

---

## 0. 리뷰 반영 요약

| 리뷰어 | 등급 | 핵심 지적 | v1.3 대응 |
|--------|------|-----------|-----------|
| **DeepSeek v1.2 검토** | A− → A | 리스크 축소 과장, Track B 일정 모순, "무중단" 잔존, 1인 팀 전제 미명시, §3.4 계약 오류 | **Track B 분리, 리스크 복원, 표현 수정, 팀규모 명시, 계약 수정** |
| **통합** | A | 동일 키 가정 오류, 용어 오류, 이름 충돌 미해결 | API 키 수정, 용어 정정, 이름 충돌 해결 |

---

## 1. 개요

### 1.1 목적
현재 `scripts/` 루트에 평평하게 배치된 50+ 진입점 스크립트와 28개 서브모듈로 구성된 `scripts/lib/`을 **업계 표준 Python 패키지 구조(src-layout + Domain-Driven Design + Ports & Adapters)**로 재구성하여, AI 에이전트와 사람이 모두 탐색하기 쉬운 코드베이스를 만든다.

**Track A (핵심 리팩토링)**: 구조 정비·안전막·기존 동작 보존 (12주)  
**Track B (LLM 공급자 추상화)**: OpenAI/Anthropic/다중 공급자 라우팅 — **`docs/LLM_PROVIDER_PLAN.md`로 별도 문서화**, v1.3 시점에 일정에서 분리

### 1.2 배경
- **현재 문제**: 진입점 분산(5개), 설정 파일 5개 분산, Shell/Python 혼재, 하드코딩 경로 40+ 곳, 순환 참조 위험, 중복/백업 파일 10개+, 심볼릭 링크(golden_image), **LLM 호출이 로컬 포트에만 의존하며 공급자 추상화 불가**
- **목표 구조**: 설치 가능한 패키지(`pip install -e .`), 단일 진입점(`devforge` CLI), 도메인별 경계 명확화, 컨테이너 친화적 빌드, **LLM 공급자 추상화 인터페이스**(Track A)

### 1.3 범위, 제약, 전제

| 항목 | 내용 |
|------|------|
| **대상** | `/opt/projects/server/scripts/` (코드), `/opt/projects/server/docs/` (문서) |
| **제외** | `_archive/` 과거 산출물, `/opt/workspace/` 외부 워크스페이스 |
| **일정** | **14주 (Phase −1 ~ Phase 7 + 안정화 2주)** |
| **팀 규모** | **2인 팀 (Tech Lead + Backend Engineer)** — 1인 팀 가정 시 20주 이상 필요 |
| **podman-py** | **비도입** — rootless 포트 포워딩 제어 불가 |
| **LLM 공급자** | Track A에서 **포트 인터페이스만 정의**, 구현은 LocalProvider. Track B는 별도 문서(`LLM_PROVIDER_PLAN.md`)로 분리 |
| **섀도 실행** | 별도 DB 스키마(`devforge_shadow`) + **replay fixture** 기반 — 프로덕션 DB/포트 경합 방지 |

---

## 2. 현재 시스템 분석 (As-Is)

### 2.1 디렉토리 구조 현황
```
/opt/projects/server/
├── scripts/                    # 루트: 50+ 파일 평평 배치
│   ├── cli.py (1965줄)         # 메인 CLI
│   ├── watchdog.py (13줄)      # 래퍼 → lib/watchdog/orchestrator.py:1076줄
│   ├── turn_watcher.py (378줄) # 파이프라인 진입점
│   ├── mcp_server.py (1486줄)  # MCP 서버
│   ├── day_cycle.sh (455줄)    # 셸 오케스트레이터 (상태 머신 + 예산 관리)
│   ├── *.bak, *.backup, *.old  # 10개+
│   ├── golden_image → /opt/workspace/...  # 심볼릭 링크 (깨짐)
│   └── lib/ (28개 서브디렉토리) # "유틸리티 창고"
│       ├── llm_client/        # MODEL_REGISTRY (포트 하드코딩)
│       ├── model_registry.py  # GGUF 메타데이터
│       ├── pod_manager/       # Podman 컨테이너 관리
│       ├── watchdog/          # 상태 머신 + recovery
│       ├── parsers/           # turn parsing
│       ├── pipeline_common/   # 공통 파이프라인 유틸
│       ├── extract_llm/       # 추출 LLM 호출
│       ├── feedback/          # 피드백 주입
│       ├── enrich/            # enrich 로직
│       ├── search/            # 검색
│       ├── research/          # exa, context7
│       ├── tracking/          # 메타데이터 추적
│       ├── output/            # 출력 포맷
│       ├── infra/             # 인프라 헬스체크
│       ├── observation/       # 관측 기록
│       ├── state_collector/   # 상태 수집
│       ├── notification/      # 알림
│       ├── action_queue/      # 액션 큐
│       ├── rubric/            # 평가 기준
│       ├── verify/            # 검증
│       ├── worklog/           # 작업 로그
│       ├── experiment_state/  # 실험 상태
│       ├── hybrid/            # 하이브리드 처리
│       ├── code_mod/          # 코드 수정
│       ├── debate/            # 토론
│       ├── proxy_utils/       # 프록시 유틸
│       ├── auth/             # 인증
│       ├── slack_interactive/  # Slack 인터랙티브
│       └── test_sandbox.py    # 테스트 샌드백
├── docs/                       # 문서 (유지)
├── containers/                 # Quadlet 컨테이너 정의
├── config/                     # 설정 템플릿
└── (pyproject.toml 없음)        # 설정 분산 (ruff.toml, pytest.ini 등)
```

### 2.2 핵심 컴포넌트 런타임 동작

| 컴포넌트 | 현재 위치 | 진입점 | 상태 관리 | 의존성 |
|----------|-----------|--------|-----------|--------|
| **Turn Collection** | `turn_watcher.py` + `lib/parsers/` | systemd service | `collect_checkpoint.json` | DB, 파일시스템 |
| **Pipeline (day_cycle)** | `day_cycle.sh` + `pipelines/*.py` | systemd timer | `pipeline_state` (DB) | Inference, DB |
| **Inference Mgmt** | `lib/pod_manager/` | `cli.py` 내부 함수 | `current-mode-inference.env` | Podman, 로컬 모델 |
| **Watchdog** | `watchdog.py` → `lib/watchdog/orchestrator.py` | systemd service | 메모리 + `watchdog_liveness` | systemd, Podman, Slack |
| **MCP Server** | `mcp_server.py` | systemd service | DB (stateless) | Inference(:8080-8084), DB |
| **LLM Client** | `lib/llm_client/__init__.py` | import by pipelines | `MODEL_REGISTRY` (포트 하드코딩) | 로컬 포트(8080-8084) |
| **Model Registry** | `lib/model_registry.py` | import by pod_manager/watchdog | GGUF 메타데이터 | 파일시스템 |
| **Config** | 5개 파일 분산 | - | - | - |

### 2.3 LLM 호출 체인 (현재)
```
lib/llm_client/__init__.py:MODEL_REGISTRY (하드코딩 포트 8080-8084)
↓
call_llm() → http://127.0.0.1:{port}/v1/chat/completions
↓
pipelines/extract.py, enrich.py, review.py, text_clean.py 등에서 직접 import
```

**한계**: 모델 → 포트 매핑이 하드코딩되어 있어, OpenAI/Anthropic API로의 라우팅이 불가능. 다른 공급자로 전환하려면 코드 수정 필요.

### 2.4 데이터 플로우 (현재)
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

### 2.5 주요 기술적 부채

| 구분 | 내용 | 영향도 |
|------|------|--------|
| **진입점 분산** | 5개 메인 진입점이 루트에 동급 배치 | AI 에이전트 탐색 실패 |
| **설정 분산** | `state.yaml`, `CLAUDE.yaml`, `secrets.env`, `current-mode-inference.env`, `current-system-mode.env` | 환경별 설정 관리 난이도 ↑ |
| **Shell/Python 혼재** | `day_cycle.sh` 455줄 + Python 파이프라인 | 테스트/디버깅/타입힌트 불가 |
| **하드코딩 경로** | `/opt/ai_data/...`, `/opt/projects/server/...` 40+ 곳 | 컨테이너 이식성 없음 |
| **순환 참조 위험** | `cli.py` → `lib.*` → `scripts.*` 양방향 | LSP 심볼 해결 실패 |
| **중복 파일** | `*.bak`, `*.backup`, `*.old` 10개+ | 에이전트 혼란 |
| **심볼릭 링크** | `golden_image → /opt/workspace/` | Git/LSP/도구 체인 깨짐 |
| **LLM 공급자 분산** | `MODEL_REGISTRY`가 로컬 포트에만 의존 | 클라우드 API 전환 불가, 키 관리 문제 |
| **pipelines/ 중복** | `pipelines/enrich.py` vs `lib/enrich/` | 코드 중복 |
| **proxy_reviewer.py** | `_archive`에 존재하지만 파이프라인에 미반영 | 사용 여부 불명 |

---

## 3. 목표 아키텍처 (To-Be)

### 3.1 설계 원칙

| 원칙 | 참조 표준 | 적용 |
|------|-----------|------|
| **src-layout** | [Python Packaging Guide](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) | `src/devforge/` 패키지, `pip install -e .` |
| **Domain-Driven Design** | Evans DDD / [eShopOnContainers](https://github.com/dotnet-architecture/eShopOnContainers) | Bounded Contexts = `domain/` 서브패키지 (비즈니스 로직만) |
| **Ports & Adapters (Hexagonal)** | [Alistair Cockburn (2005)](https://alistair.cockburn.us/hexagonal-architecture/) | `ports/` = Protocol, `adapters/` = 구현체, `domain/` = Core |
| **Single Entry Point** | Typer Best Practices | `devforge` CLI + `pyproject.toml` entry_points (`main.py` 제거) |
| **Configuration as Code** | [12-Factor](https://12factor.net/) / [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) | `ConfigRegistry` 패턴, 파일 물리 분리 유지 |
| **Container-First** | [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) | 멀티스테이지 Dockerfile, 단일 이미지 다중 진입점 |
| **Observability** | OpenTelemetry / Structured Logging | JSON 로깅, 메트릭 엔드포인트 |
| **Test-First Migration** | Michael Feathers, *Working Effectively with Legacy Code* | 특성화 테스트 → 리팩토링 → 회귀 테스트 |
| **Dual Track** | Feature Branch + Feature Flag | Track A: 14주(리팩토링), Track B: 별도 문서화 |
| **Shadow Testing** | Martin Fowler, *Regression Testing* | 섀도 DB + replay 하네스로 구/신 대조 |

### 3.2 목표 디렉토리 구조 (최종 확정본)

```
/opt/projects/server/
├── pyproject.toml
├── Dockerfile
├── README.md
├── LICENSE
├── src/
│   └── devforge/
│       ├── __init__.py
│       ├── cli.py                  # Typer 단일 진입점
│       ├── core/                   # 공통 인프라
│       │   ├── __init__.py
│       │   ├── config.py           # ConfigRegistry (BaseSettings)
│       │   ├── database.py         # SQLAlchemy 2.0 async pool
│       │   ├── logging.py          # JSON 구조화 로깅
│       │   ├── paths.py            # 하드코딩 경로 추상화 (40곳 해결)
│       │   └── exceptions.py
│       ├── ports/                  # 인터페이스 정의 (Protocols)
│       │   ├── __init__.py
│       │   ├── inference.py        # LLMProvider Protocol
│       │   ├── storage.py          # StoragePort Protocol
│       │   └── container.py        # ContainerManager Protocol
│       ├── domain/                 # 비즈니스 도메인 (Bounded Contexts)
│       │   ├── __init__.py
│       │   ├── turn_collection/    # parsers, watcher, collector
│       │   ├── pipeline/           # extract, enrich, embed, review 로직
│       │   ├── watchdog/           # health checks, recovery
│       │   └── model_management/   # 모델 메타데이터 (GGUF)
│       ├── adapters/               # 외부 기술 구현체
│       │   ├── __init__.py
│       │   ├── driven/
│       │   │   ├── __init__.py
│       │   │   ├── llm/            # LLM 공급자 어댑터
│       │   │   │   ├── local.py    # 로컬 포트 (8080-8084) — Track A 기본
│       │   │   │   ├── __init__.py
│       │   │   └── factory.py       # ProviderFactory (Track A: Local only)
│       │   │   ├── container/      # Podman subprocess 구현
│       │   │   ├── storage/        # OCI Object Storage
│       │   │   ├── notification/   # Slack, Telegram, Apprise
│       │   │   └── research/       # exa, context7
│       │   └── driving/
│       │       ├── __init__.py
│       │       ├── mcp/            # FastMCP + Tools (8개 네임스페이스)
│       │       ├── cli_cmds/       # CLI 서브커맨드
│       │       └── proxies/        # LLM API 프록시
│       ├── application/            # 애플리케이션 서비스 / 오케스트레이션
│       │   ├── __init__.py
│       │   ├── orchestrator.py     # PipelineOrchestrator (BudgetManager 포함)
│       │   ├── day_cycle.py        # 일일 사이클 실행
│       │   ├── agent_interface.py  # Web/CLI/Scheduled 추상화
│       │   └── issue_collector.py  # 배치 리뷰 시스템
│       └── pipeline_stages/        # 파이프라인 실행 모듈 (domain 로직 호출)
│           ├── __init__.py
│           ├── extract.py
│           ├── enrich.py
│           ├── embed.py
│           └── review.py
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── characterization/           # Phase 0.5: 기존 동작 보호 + record/replay
│   ├── fixtures/                   # LLM 응답 capture/replay fixtures
│   └── conftest.py
├── scripts/                      # 운영/배포 스크립트 (진입점 X)
├── docs/
├── containers/                   # Quadlet 파일
├── config/
│   ├── secrets.env.example
│   ├── providers.example.yaml    # Track B: LLM 공급자 (별도 문서화 중심)
│   ├── state.yaml.example
│   └── CLAUDE.yaml.example
└── .github/workflows/
```

### 3.3 이름 충돌 해결 (최종 확정)

| v1.0/v1.1 | v1.3 | 이유 |
|-----------|------|------|
| `interfaces/` (adapters 폴더) | `adapters/driving/` + `adapters/driven/` | Hexagonal 원칙: driving vs driven 구분 |
| `interfaces/cli/` | `adapters/driving/cli_cmds/` | CLI는 driving adapter |
| `interfaces/mcp/tools/` | `adapters/driving/mcp/tools/` | MCP도 driving adapter |
| `pipelines/` (최상위) + `lib/enrich/` + `lib/extract_llm/` | `pipeline_stages/` (domain pipeline 실행), `domain/pipeline/stages/` (순수 로직) | **3중 분리 해결**: `pipelines/`는 `application/` → `pipeline_stages/`로 이동. 도메인 로직은 `domain/pipeline/stages/`에 보관 |

**`pipelines/`는 문서 어디에도 등장하지 않습니다.** 완전히 `pipeline_stages/`와 `application/orchestrator.py`로 대체되었습니다.

### 3.4 핵심 인터페이스 계약 (수정본)

```python
# src/devforge/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class ModelProvidersConfig(BaseSettings):  # FIXED: BaseModel → BaseSettings
    """Track A: LLM 공급자 설정 — 기본값은 Local"""
    model_config = SettingsConfigDict(env_prefix="DEVFORGE_")
    
    default_provider: str = "local"  # Track A 기본값 (Track B는 별도 문서화)
    
    def resolve_provider_name(self, model_key: str) -> str:
        """model_key에 매핑된 provider 이름 반환. Track A에서는 항상 'local'."""
        return self.default_provider  # Track B에서만 다중 provider로 확장

# src/devforge/ports/inference.py
from typing import Protocol

class LLMProvider(Protocol):
    provider_name: str
    api_base: str
    
    async def chat(self, messages: list[dict], **kwargs) -> LLMResult: ...
    async def embeddings(self, texts: list[str]) -> list[list[float]]: ...

# src/devforge/adapters/driven/llm/local.py (Track A — 기존 기능 보존)
class LocalLLMProvider:
    """현재 MODEL_REGISTRY 기반 포트 호출. Track A 유일하게 사용."""
    def __init__(self, port: int, model_name: str): ...

# src/devforge/adapters/driven/llm/factory.py
def create_llm_provider(provider_name: str, config: ModelProvidersConfig) -> LLMProvider:
    """Track A: local만 반환. Track B: openai/anthropic 추가 (별도 문서화)."""
    if provider_name == "local":
        return LocalLLMProvider(...)
    # Track B 구현은 docs/LLM_PROVIDER_PLAN.md 참조
    raise NotImplementedError(f"Provider '{provider_name}' not implemented in Track A")

# src/devforge/application/orchestrator.py
class PipelineOrchestrator:
    def __init__(self, llm_provider: LLMProvider, budget_seconds: int = 21600):
        self.llm = llm_provider
        self.budget = BudgetManager(budget_seconds)  # BudgetManager 복원
```

**수정 내역**:
1. `BaseModel` → `BaseSettings` (Pydantic Settings가 맞는 API)
2. `get_provider()` → `resolve_provider_name()` (core가 adapter를 반환하면 순환 참조 위반)
3. `cloudahq` 오타 수정 (Track B는 별도 문서화)
4. `BudgetManager` 복원 (v1.1에서 지적당 항목)

---

## 4. 실행 계획 (14주, Track A 전념)

### Phase −1: 정리 및 기저장치 캡처 (Week 0.5, 2일)

| 작업 | 산출물 | 검증 |
|------|--------|------|
| 심볼릭 링크 정리 (golden_image) | 로컬 복사본 | `ls scripts/golden_image` → 파일 |
| `*.bak`/`.backup`/`.old` 정리 (10개+) | `_archive/` 이동 | `find . -name "*.bak"` → 0 |
| 하드코딩 경로 인벤토리 (40+) | `data/hardcoded_paths.csv` | 경로 목록 확보 |
| `day_cycle.sh` 행위 명세화 (455줄) | `data/day_cycle_behavior.md` | 상태 전이·예산 로직 명세 |
| 골든마스터 캡처 (DB 상태 + checkpoint + 이미지) | `data/golden_master.json` | `SELECT pipeline_state, COUNT(*) FROM turns GROUP BY ...` |
| **LLM 응답 캡처 (record/replay 하네스)** | `tests/fixtures/llm_recordings/` | 5개 핵심 경로 LLM 응답 fixture 확보 |

**완료**: 골든마스터 + record/replay 하네스 (구현 + 캡처). **LLM 캡처는 여기서 반드시 진행** (Phase 3 이후엔 구 코드가 사라짐).

### Phase 0: 기반 구축 + 특성화 테스트 (Week 1-2)

| 주차 | 작업 | 산출물 | 검증 |
|------|------|--------|------|
| **Week 1** | `pyproject.toml` + `src/devforge/` 골격 | 30+ `__init__.py` | `pip install -e .` 성공 |
| | `ConfigRegistry` (5개 파일 통합, `Paths` 추상화) | `core/config.py`, `core/paths.py` | 기존 5개 파일 파싱 |
| | `DatabaseGateway` + Alembic 설정 | `core/database.py`, `alembic/` | `alembic upgrade head` 성공 |
| | 로깅 표준화 (structlog + JSON) | `core/logging.py` | stdout JSON 출력 |
| | `import-linter` CI 게이트 | `pyproject.toml` | `linter` 0 violations |
| **Week 2** | **특성화 테스트 작성 (5개 핵심 경로)** | `tests/characterization/` | - `day_cycle.sh` 배치 예약 로직 (scanned count = 10) <br> - `check_all_llm` T1/T2 probe (포트 8082 활성화 시 200) <br> - `call_llm` 응답 형식 + **응답 내용 고정** <br> - `watchdog` 60초 루프 (liveness_ts 업데이트) <br> - `text_clean` 언어 감지 (한국어 텍스트 정제 결과 고정) |

**진입**: Phase −1 완료 (골든마스터 + LLM 캡처 필수)  
**완료**: `devforge --help` 동작, 특성화 테스트 5/5 통과

### Phase 1: 추론 컨테이너 도메인화 + LLM Provider 포트 (Week 3-4)

| 주차 | 작업 | 산출물 | 검증 |
|------|------|--------|------|
| **Week 3** | `InferenceContainerManager` 포트 + `SubprocessImpl` | `ports/container.py` | 컨테이너 기동/정지/헬스체크 성공 |
| | `ModelRegistry` 도메인화 | `domain/model_management/registry.py` | `MODEL_METADATA` 조회 성공 |
| | **`LLMProvider` 포트 정의** + `LocalLLMProvider` 구현 | `ports/inference.py` + `adapters/driven/llm/local.py` | 로컬 포트 호출 성공 (DI 통해 주입) |
| | `cli.py` 내 inference 서브커맨드 이전 | `adapters/driving/cli_cmds/inference.py` | `devforge inference switch day` 동작 |
| **Week 4** | `PipelineOrchestrator` 스케치 (BudgetManager 포함) | `application/orchestrator.py` | `budget_gate()` 동작 |
| | 하드코딩 경로 40곳 검증 | `core/paths.py` | `Paths.data_dir` 오버라이드 가능 |
| | Phase 1.5 ADR 작성 | `docs/adr/0003-shadow-db.md` | 스키마 설계 결정문서 |

### Phase 1.5: 섀도 DB + Replay 하네스 (Week 4.5, 2일)

| 작업 | 산출물 | 검증 |
|------|--------|------|
| 섀도 DB 스키마 설계 (독립 스키마, Alembic 동일 리비전) | `docs/adr/0003-shadow-db.md` | DDL 검토 |
| `turns_shadow` 테이블 생성 (읽기 전용 뷰) | `sql/shadow_schema.sql` | `SELECT COUNT(*) FROM turns_shadow` |
| replay 하네스 검증 (fixture → API 호출 대체) | `tests/fixtures/replay_harness.py` | 섀도 day_cycle이 replay 사용 |

**목적**: Phase 3의 구/신 비교가 **같은 DB에서 경합하지 않도록** + **LLM 응답 비재현성 해결**

### Phase 2: Watchdog 도메인화 + IssueCollector (Week 5)

| 작업 | 산출물 | 검증 |
|------|--------|------|
| `WatchdogOrchestrator` 클래스화 | `domain/watchdog/orchestrator.py` | 60초 루프 정상 동작 |
| `ComponentTracker` + `GraduatedRecovery` | `domain/watchdog/recovery.py` | 백오프/서킷브레이커 보존 |
| `IssueCollector` 구현 | `application/issue_collector.py` + `collected_issues` 테이블 | 자동 수집 트리거 동작 |
| MCP 도구 `watchdog_*` 분리 | `adapters/driving/mcp/tools/watchdog/` | `devforge watchdog status` 동작 |

### Phase 3: 파이프라인 도메인화 + 오케스트레이터 (Week 6-7)

| 주차 | 작업 | 산출물 |
|------|------|--------|
| **Week 6** | 파이프라인 단계별 모듈화 | `domain/pipeline/stages/{extract,enrich,embed,review}.py` |
| | `PipelineOrchestrator` 구현 (BudgetManager 포함) | `application/orchestrator.py` |
| | `day_cycle.sh` → `PipelineOrchestrator.run_full_cycle()` | `application/day_cycle.py` |
| | LLM Provider DI (Track A: Local) | `pipeline_stages/{extract,enrich}.py`에서 `self.llm.chat()` 호출 |
| **Week 7** | MCP 도구 `pipeline_*` 7개 | `adapters/driving/mcp/tools/pipeline/` |
| | orchestrate/status/resume/budget_gate | `adapters/driving/mcp/tools/orchestration/` |
| | `day_cycle.sh` → `devforge pipeline orchestrate` 래퍼 | `scripts/day_cycle.sh` (10줄) |

### Phase 3.5: 병렬 검증 (Week 8-9, **2주 확보**)

| 작업 | 검증 |
|------|------|
| **섀도 day_cycle 시작** (devforge_shadow DB + replay) | 프로덕션 DB에 쓰지 않음 |
| **구/신 상태 대조 (2주, 최소 14 사이클)** | - 결정론적 단계: diff = 0<br>- 확률적 단계: 분포 검정 (p < 0.05) |
| Track A LLM Provider 검증 (Local + replay) | 응답 시간 < 10s, 품질 동일 (fixture 대조) |
| 롤백 테스트 (Quadlet digest 고정) | 5분 내 롤백 성공 |

**Week 8-9 할당 이유**: day_cycle은 일 1회이므로 2주야 `n ≥ 14` 샘플 확보 가능. 1주(Review에서 지적)면 n≈7으로 통계 검정 불가.

### Phase 4: Turn Collection + MCP 재구성 (Week 10)

| 작업 | 산출물 | 검증 |
|------|--------|------|
| `TurnWatcher` 클래스화 | `domain/turn_collection/` | 3초 폴링, 체크포인트 보존 |
| MCP 도구 18개 → 8개 네임스페이스 | `adapters/driving/mcp/tools/{knowledge,pipeline,inference,actions,watchdog,deepdive}/` | `devforge mcp tools` 계층적 탐색 |
| `AgentInterface` 추상화 | `application/agent_interface.py` | Web/CLI/Scheduled 공통 인터페이스 |
| 배치 리뷰 시스템 | `application/issue_collector.py` + tools | P0/P1 자동 수집 → 주간 리포트 |

### Phase 5: 잔여 도메인 + 인터페이스 (Week 11-12)

| 주차 | 작업 | 산출물 |
|------|------|--------|
| **Week 11** | `storage/` (OCI SDK, FileRegistry) | `adapters/driven/storage/` |
| | `notification/` (Slack/Telegram/Apprise) | `adapters/driven/notification/` |
| | `file_exchange/` (blob_explorer) | `adapters/driven/` |
| **Week 12** | `research/` (exa/context7/web) | `adapters/driven/research/` |
| | `proxy_utils/` (게이트웨이) | `adapters/driven/proxy_utils/` |

**조정**: Track B(LLM Provider) 제외 → 4개 도메인을 2주로 재배치

### Phase 6: 컨테이너화 + CI/CD (Week 13)

| 작업 | 산출물 |
|------|--------|
| 멀티스테이지 Dockerfile | 단일 이미지, 다중 진입점 |
| Quadlet (`Image=localhost/devforge:latest`**digest 고정**) | systemd 재시작 + 5분 롤백 가능 |
| GitHub Actions CI (ruff, mypy, pytest, import-linter) | PR 검증 |
| `docs/ARCHITECTURE.md` | 새 구조 반영 |
| `docs/MIGRATION_GUIDE.md` | 팀 온보딩용 |
| `docs/ADR/0001-config-priority.md` | ConfigRegistry 우선순위 |
| `docs/ADR/0002-llm-provider-flag.md` | Provider 추상화 결정 (Track B는 별도) |

### Phase 7: 안정화 + 롤백 + 정리 (Week 14)

| 작업 | 검증 |
|------|------|
| 기존 `scripts/` 임포트 루트 제거 | `python -c "import lib"` → ImportError |
| 첫 주간 리포트 (collected_issues) | 리포트 생성 테스트 |
| 문서 동기화 (ARCHITECTURE, API_REFERENCE) | 최신 구조 반영 |
| 롤백 훈련 (Quadlet digest 고정) | 5분 내 롤백 검증 |
| `day_cycle.sh` → `devforge pipeline orchestrate` | 래퍼 10줄 검증 |
| **안정화 (2주)** | Bug triage, team 적응 |

> **안정화 기간 (14주 이후, 2주)**: 리팩토링 후버그 수정, 팀 온보딩, 문서 보강. **KPI 측정 시작**.

---

## 5. 업계 표준 비교

### 5.1 프로젝트 구조
| 측면 | 목표 | 업계 표준 |
|------|------|-----------|
| 패키지 레이아웃 | src-layout | [Python Packaging Guide](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) |
| 진입점 | `pyproject.toml` entry_points | Typer CLI 모범 |
| 설정 | ConfigRegistry (BaseSettings) | [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) |
| 아키텍처 | Ports & Adapters | [Cockburn Hexagonal (2005)](https://alistair.cockburn.us/hexagonal-architecture/) |
| 테스트 | Characterization + replay | [Feathers *WELAC*](https://www.goodreads.com/book/show/398787.Working_Effectively_with_Legacy_Code) |
| 컨테이너 | Quadlet + digest 고정 | [Podman Quadlet](https://docs.podman.io/en/latest/markdown/podman-systemd.unit.5.html) |
| 의존성 | import-linter 게이트 | [ import-linter  — architecture enforcement](https://github.com/import-linter/import-linter) |

### 5.2 LLM 공급자 아키텍처 (Track A — 포트만 정의)

| 패턴 | 구현 |
|------|------|
| **Protocol** | `LLMProvider` (ports/inference.py) — `chat()`, `embeddings()` |
| **Local** | `LocalLLMProvider` (adapters/driven/llm/local.py) — 포트 기반 |
| **Factory** | `create_llm_provider()` (adapters/driven/llm/factory.py) — Track A: local only |
| **Feature Flag** | `DEVFORGE_LLM_PROVIDER` env var (추후 Track B 추가) |
| **DI** | `PipelineOrchestrator(llm_provider=...)` | 설정 → Provider 생성 → 주입 |

**API 키**: OpenAI와 Anthropic은 **별도 키**가 필요합니다. 동일 키를 사용하는 것은 OpenRouter/LiteLLM 같은 게이트웨이를 거쳤을 때 가능하며, 이는 `docs/LLM_PROVIDER_PLAN.md`(Track B 별도 문서)에서 논의 예정입니다.

> **Track B(Cloud 공급자)**는 별도 문서(`docs/LLM_PROVIDER_PLAN.md`)에서 계획 및 검토 예정입니다. v1.3 시점에서는 Track A의 LocalProvider 인터페이스만 확정하고 구현합니다.

### 5.3 테스트 전략
| 테스트 종류 | 목적 | 도구 |
|-------------|------|------|
| Characterization | 기존 동작 보존 | pytest + replay 하네스 |
| Replay Fixture | LLM 응답 결정론화 | `tests/fixtures/llm_recordings/` |
| Unit | 단위 검증 | pytest |
| Integration | 통합 검증 | pytest + Docker |
| Architecture | 의존성 방향 | import-linter |
| Shadow E2E | 구/신 대조 | day_cycle_shadow.sh + devforge_shadow DB |

---

## 6. 리스크 (v1.1 표 복원 + 신규 추가)

| 리스크 | 가능성 | 영향 | 완화 | Status |
|--------|--------|------|------|--------|
| **프로덕션 DB 경합** | High | Critical | **섀도 DB 분리 (Phase 1.5)** + replay 하네스 | Mitigated (Phase 1.5) |
| **LLM 응답 비재현성** | High | High | **record/replay 하네스 (Phase −1)** | Mitigated (Phase −1) |
| **day_cycle.sh 로직 누락 (455줄)** | Medium | High | **행위 명세 (Phase −1)** + 2주 병렬 검증 | In Progress (Phase 3.5) |
| **데이터 손실 (pipeline_state)** | Low | Critical | **Alembic + expand/contract** (ADR로 규정) | To Do (Phase 0) |
| **기존 시스템 중단** | Medium | Critical | **Feature flag + 섀도 DB + 단계적 전환** | Partial (Phase −1 ~ 3.5) |
| **순환 참조 재발** | Medium | High | **import-linter CI 게이트** | Mitigated (Phase 0) |
| **하드코딩 경로 미해결** | Medium | High | **`Paths` 추상화 (Phase 0)** + 검증 | In Progress (Phase 0) |
| **LLM 공급자 전환 (Track B)** | Medium | Medium | **Feature Flag + 별도 문서화** | Planning (docs/LLM_PROVIDER_PLAN.md) |
| **podman-py rootless** | High | Medium | **비도입 확정** | Mitigated (결정됨) |
| **MCP 도구 인터페이스 변경** | Low | Medium | **도구명 별칭 + 에이전트 회귀 테스트** | To Do (Phase 4) |
| **팀 학습 곡선** | High | Medium | **문서화 + 페어 프로그래밍 + 2인 팀** | Monitoring (전체) |

> **v1.1에서 삭제된 Critical/High 리스크 4건 재추가**:
> - 프로덕션 DB 경합 → **섀도 DB + replay**으로 해결됨 (Status: Mitigated)
> - 데이터 손실 → **Alembic + expand/contract**으로 부분 해결 (Status: To Do)
> - 기존 시스템 중단 → **Feature flag**으로 완화 (Status: Partial)
> - 순환 참조 → **import-linter**으로 해결 (Status: Mitigated)

---

## 7. KPI

| 지표 | 현재 | 목표 | 측정 방법 |
|------|------|------|---------|
| AI 에이전트 진입점 탐색 시간 | >5분 (50개 파일 중 추측) | <10초 (`devforge --help`) | **측정 프로토콜**: Claude Code 세션 3회, 프롬프트 "이 프로젝트에서 파이프라인 실행 파일을 찾아줘", 10초 이내 파일 1개 특정 |
| LSP 심볼 해결 성공률 | ~60% (순환 참조) | >95% | `pyright --outputjson` + import-linter 0 violations |
| 컨테이너 빌드 시간 | ~3분 (스크립트 복사) | <1분 (wheel 캐시) | CI 로그 |
| 롤백 시간 | 30분 (수동) | **<10분 (Quadlet digest 고정)** | 리졸루션: "무중단"에서 "5분 내 롤백 가능"으로 수정 |
| 테스트 커버리지 (신규) | 0% | >80% (unit + integration + characterization) | `pytest --cov` (신규 코드 기준) |
| 타입 힌트 커버리지 | ~20% | >90% | `mypy --strict` |
| 하드코딩 경로 | 40+ | 0 | `grep "/opt/" src/` → 0 (`core/paths.py` 예외) |
| 섀도 검증 diff | N/A | 결정론 0% | Week 8-9 대조 (n≥14 샘플) |

---

## 8. 문서화 계획

| 문서 | 시점 |
|------|------|
| `REFACTORING_PLAN.md` (v1.3) | ✅ 작성 완료 |
| `ARCHITECTURE.md` | Phase 1 완료 |
| `MIGRATION_GUIDE.md` | Phase 3 완료 |
| `LLM_PROVIDERS.md` | Track B 문서화 시 (별도) |
| `API_REFERENCE.md` | Phase 4 완료 |
| `OPERATIONS_GUIDE.md` | Phase 6 완료 |
| `ADR/0001-config-priority.md` | Phase 0 완료 |
| `ADR/0002-llm-provider-flag.md` | Phase 1 완료 |
| `ADR/0003-shadow-db.md` | Phase 1.5 완료 |
| `ADR/0004-alembic-migrate.md` | Phase 0 완료 |

---

## 9. 용어 정의

| 용어 | 정의 |
|------|------|
| **Ports & Adapters** | 핵심 로직(Port = Protocol)과 외부 기술(Adapter) 분리 — Cockburn (2005) |
| **Track A / Track B** | Track A: 리팩토링 (핵심), Track B: LLM 공급자 (별도 문서화) |
| **Characterization Test** | 리팩토링 전 기존 동작 고정 — Feathers, *Working Effectively with Legacy Code* |
| **Shadow DB** | 프로덕션 DB와 분리된 검증 전용 스키마 |
| **record/replay 하네스** | LLM 응답 캡처 → 재생으로 결정론화 |
| **expand/contract** | DB 마이그레이션 전략 — 새 컬럼 추가(additive) 후 단계적 삭제 |

---

## 10. 승인

| 역할 | 이름 | 서명 | 날짜 |
|------|------|------|------|
| Technical Lead | | | |
| DevOps Lead | | | |
| Backend Engineer | | | |

---

> **참고**: 이 문서는 살아있는 문서입니다. 각 Phase 완료 시 실제 구현 내용에 맞춰 업데이트하며, `docs/adr/`에 주요 결정 사항을 별도 기록합니다. **Track B(LLM 공급자)**는 `docs/LLM_PROVIDER_PLAN.md`로 분리하여 별도 검토 및 계획 수립 예정입니다.
