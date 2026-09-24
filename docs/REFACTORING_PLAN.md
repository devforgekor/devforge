# DevForge 서버 리팩토링 종합 계획서 v1.5

> Status: active · Date: 2026-09-23 · Owner: devforge · Related: `docs/ARCHITECTURE.md`, `docs/MIGRATION_GUIDE.md`, `docs/adr/`, `docs/refactoring/REFACTORING_STATUS.yaml`
> **연계(운영 아키텍처, 별도 정본)**: `plans/system-reference-architecture.md`(목표 운영/제어 아키텍처) · `plans/detection-remediation-architecture.md`(감지→수정) · `plans/fitness-functions-heartbeat-drift-guide.md`(검증). 본 계획=코드 재구성, 위=운영 아키텍처(컷오버 후 승격).

> **버전**: 1.5 (실행 현황 반영)
> **상태**: Active — Phase 0/1/1.5 완료, **Phase 2 shadow-run(2.5) 진행 중**
> **Changelog**: v1.0→v1.1: 12주→14주, 특성화 테스트 | v1.1→v1.2: Track B 분리, 섀도 DB | v1.2→v1.3: 리스크 복원, 팀규모 | v1.3→v1.4: 일정/표현 정합성 수정 | v1.4→v1.5: 페이즈 표기 통일(`Phase <N>[.<M>]`), Phase 0~2 실행 현황·목표구조 현행화

> **페이즈 표기(정본)**: `Phase <N>[.<M>]` — N=로드맵 단계(0~8), M=내부 단계(0=구현, 1~4=검증 Gate, 5=shadow/병렬 run, 9=컷오버). `Gate k`·컷오버 `Phase A~I`는 phase 내부 라벨이며 별도 phase가 아니다. 기계판독 진행 현황은 `docs/refactoring/REFACTORING_STATUS.yaml`.

---

## 0. 리뷰 반영 이력

| 버전 | 리뷰어 | 핵심 지적 | 대응 |
|------|--------|-----------|------|
| v1.0→v1.1 | DeepSeek | 일정 낙관, 테스트 공수 0, podman-py 리스크, 설정 마이그레이션 미흡 | 12주→14주, Phase 0.5 특성화 테스트 |
| v1.1→v1.2 | Claude #2 | architecture 위반(domain에 adapter), 이름 충돌, 테스트 공수 0 | ports/adapters 분리, pipelines 개명 |
| v1.2→v1.3 | DeepSeek | 리스크 과장, Track B 일정 모순, "무중단" 잔존, 팀규모 미명시 | Track B 분리, 리스크 복원, 표현 수정 |
| v1.3→v1.4 | Claude #2 | 일정 합계 불일치, Phase 5/7 밀도, 롤백 시간 단위 | **총 16주 명시**, **Phase 8 승격**, **<5분 통일** |

> **v1.4는 최종 검토를 거친 실행 계획서입니다.** 이력은 위 표에만 기록되며 본문은 단일 버전으로 유지합니다.

---

## 1. 개요

### 1.1 목적
현재 `scripts/` 루트에 평평하게 배치된 50+ 진입점 스크립트와 28개 서브모듈로 구성된 `scripts/lib/`을 **업계 표준 Python 패키지 구조(src-layout + Domain-Driven Design + Ports & Adapters)**로 재구성하여, AI 에이전트와 사람이 모두 탐색하기 쉬운 코드베이스를 만든다.

**Track A (핵심 리팩토링)**: 구조 정비·안전막·기존 동작 보존 (14주)  
**Track B (LLM 공급자 추상화)**: OpenAI/Anthropic/다중 공급자 라우팅 — **`docs/LLM_PROVIDER_PLAN.md`로 별도 문서화**

### 1.2 배경
- **현재 문제**: 진입점 분산(5개), 설정 파일 5개 분산, Shell/Python 혼재, 하드코딩 경로 40+ 곳, 순환 참조 위험, 중복/백업 파일 10개+, 심볼릭 링크(golden_image), **LLM 호출이 로컬 포트에만 의존하며 공급자 추상화 불가**
- **목표 구조**: 설치 가능한 패키지(`pip install -e .`), 단일 진입점(`devforge` CLI), 도메인별 경계 명확화, 컨테이너 친화적 빌드, **LLM 공급자 추상화 인터페이스**(Track A)

### 1.3 범위, 제약, 전제

| 항목 | 내용 |
|------|------|
| **대상** | `/opt/projects/server/scripts/` (코드), `/opt/projects/server/docs/` (문서) |
| **제외** | `_archive/` 과거 산출물, `/opt/workspace/` 외부 워크스페이스 |
| **일정** | **계획 총 17주 (Phase −1 ~ Phase 8)** (Phase −1: 2일, Phase 0~7: 14주, Phase 8: 2주). **실제**: Phase 0~2를 2026-09-13~09-23(11일)에 압축 진행 — 잔여는 §4.0 재기준 |
| **팀 규모** | **2인 팀 (Tech Lead + Backend Engineer)** — 1인 팀 가정 시 20주 이상 필요 |
| **podman-py** | **비도입** — rootless 포트 포워딩 제어 불가 |
| **LLM 공급자** | Track A에서 **포트 인터페이스만 정의**, 구현은 LocalProvider. Track B는 별도 문서(`LLM_PROVIDER_PLAN.md`)로 분리 |
| **섀도 실행** | 별도 DB 스키마(`devforge_shadow`) + **replay fixture** 기반 — 프로덕션 DB/포트 경합 방지 |

---

## 2. 현재 시스템 분석 (As-Is)

> **기준 시점(2026-09-14) 스냅샷.** 현재 `scripts/`는 **컷오버 전까지 병존하는 레거시**이며(라이브 유닛 30개가 아직 `scripts/*` 실행), 신규 로직은 `src/devforge/`로 이전 중이다. 최신 실측은 `docs/refactoring/REFACTORING_STATUS.yaml`을 본다.

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
| **웹 LLM 수집 미연결** | `chrome-web-llm` CLI 운영 중(Qwen/DeepSeek 질의·세션/핸드오프)이나 대화가 파이프라인(`turn_collection`/`ingest`)에 미유입 | 웹 대화가 지식베이스에 반영 안 됨 → Phase B(ADR-0006) |

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
│       ├── cli.py                  # ✅ Typer 단일 진입점 (composition root)
│       ├── core/                   # ✅ config, database, logging, paths, exceptions
│       ├── ports/                  # ✅ extract, container, health_check, heartbeat,
│       │                           #    incident_repository, notification, recovery,
│       │                           #    state_persistence, types
│       ├── domain/                 # ✅ Bounded Contexts
│       │   ├── model_management/   #    모델 메타데이터 (GGUF)
│       │   ├── turn_collection/    #    parsers, watcher, collector
│       │   ├── pipeline/stages/    #    extract, enrich, embed, review
│       │   └── watchdog/           #    monitoring(tracker,backoff) / orchestration /
│       │                           #    recovery(graduation,strategies)
│       ├── adapters/
│       │   ├── driven/             # ✅ llm(local_adapter),
│       │   │                       #    storage(database_gateway, extract_adapter,
│       │   │                       #    incident_pg, state_json, heartbeat_pg),
│       │   │                       #    health(6), container(podman_adapter),
│       │   │                       #    recovery(systemd_recovery),
│       │   │                       #    notification(slack,systemd), research, proxy_utils
│       │   └── driving/            # ✅ api, mcp, cli_cmds
│       ├── application/            # ✅ extract_pipeline, orchestrator, watchdog_service
│       │                           # ⬜ day_cycle, agent_interface, issue_collector
│       └── pipeline_stages/        # ✅ extract/  ⬜ enrich, embed, review
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

# src/devforge/ports/extract.py
from abc import ABC, abstractmethod

class LLMPort(ABC):
    provider_name: str
    api_base: str

    @abstractmethod
    async def chat(self, messages: list[dict], model_key: str = "day_extract",
                   json_mode: bool = False, **kwargs) -> dict[str, Any]: ...
    @abstractmethod
    async def verify_claim(self, claim: str, evidence: str, **kwargs) -> dict[str, Any]: ...

# src/devforge/adapters/driven/llm/local_adapter.py (Track A — 기존 기능 보존)
class LocalLLMAdapter(LLMPort):
    """현재 MODEL_REGISTRY 기반 포트 호출. Track A 유일하게 사용."""
    def __init__(self, model_registry: dict | None = None): ...

# Provider 선택: src/devforge/core/config.py 의 config.llm_provider
#   (DEVFORGE_LLM_PROVIDER env로 오버라이드, Track A 기본 'local')
# factory는 Track B에서 도입 — docs/LLM_PROVIDER_PLAN.md (미착수)

# src/devforge/application/extract_pipeline.py  (Phase 1 구현체; PipelineOrchestrator는 Phase 3 예정)
class ExtractPipeline:
    def __init__(self, llm: LLMPort, db: ExtractPort,
                 turn_repo: TurnRepository | None = None,
                 batch_limit: int = 50, dry_run: bool = False):
        self.llm = llm
        ...
```

**수정 내역**:
1. `BaseModel` → `BaseSettings` (Pydantic Settings가 맞는 API)
2. `get_provider()` → `resolve_provider_name()` (core가 adapter를 반환하면 순환 참조 위반)
3. "cloudahq" 오타 수정 → Track B는 별도 문서화로 분리
4. `LLMProvider`/`ports/inference.py` → `LLMPort`/`ports/extract.py` 명칭·경로 정정 (ADR-0002와 일치), `LocalLLMAdapter`/`local_adapter.py` 반영

---

## 4. 실행 계획 (계획 17주 → 실제 기준 재기준, Phase −1~8)

### 4.0 실제 vs 계획 (실행 중 변경 반영, 2026-09-23 기준)

| Phase | 계획 | 실제 | 상태 | 실행 중 변경(스코프) |
|---|---|---|---|---|
| −1 정리·캡처 | Week 0.5 (2일) | ~09-13 | ✅ 완료 | 산출물 6종 확인(`data/{golden_master.json,day_cycle_behavior.md,hardcoded_paths.csv}`, `tests/fixtures/{llm_recordings(6),replay_harness.py}`, `sql/shadow_schema.sql`); `scripts/*.bak`=0, `golden_image` 심링크→디렉토리 |
| 0 기반·특성화 | Week 1-2 (~09-27) | 09-13 → **09-21** (9일) | ✅ 완료 | 6일 조기 완료. `core/database.py`는 09-22 dead code 제거 → `adapters/driven/storage/database_gateway.py`로 이전 |
| 1 추론/LLM 포트 | Week 3-4 | 09-21 → **09-22** | ✅ 완료 | 계획 항목 전량 완료 |
| 1.5 섀도DB·replay | Week 4.5 (2일) | ~09-20 → 09-22 | ✅ 완료 | `devforge_shadow` 라이브 |
| 2 Watchdog | Week 5 (10 tasks) | 09-22 → **09-23** | 🟡 **2.5 shadow-run** | **대폭 확장**: Gate 1~4 + v2.1 **18 tasks(A1–E3)**. `IssueCollector`·MCP `watchdog_*` 분리는 **잔여** |
| 3 파이프라인 | Week 6-7 | 재기준 | ⬜ 승인(D6=A) | **범위 축소**: devforge는 **embed** 단계만 소유 |
| 3.5 병렬 검증 | Week 8-9 (2주) | — | ⬜ | 2주(n≥14) 유지 — 통계 검정 요건 |
| 4~8 | Week 10-16 | 일부 선행 | ⬜/부분 | storage·notification·research·proxy_utils, `Dockerfile`·CI·`ARCHITECTURE`·`MIGRATION_GUIDE` **선행 구현** |

> **재기준 원칙**: Phase 0~2는 계획 대비 압축 진행(2주→9일 등)됐으므로 잔여(Phase 3 이후)는 캘린더 주차가 아니라 **의존성·검증 요건**(예: Phase 3.5의 2주 대조, Phase 2.9 컷오버)으로 일정을 산정한다.

### Phase −1: 정리 및 기저장치 캡처 (Week 0.5, 2일) ✅ 완료

| 작업 | 산출물 | 검증 |
|------|--------|------|
| 심볼릭 링크 정리 (golden_image) | 로컬 복사본 | `ls scripts/golden_image` → 파일 |
| `*.bak`/`.backup`/`.old` 정리 (10개+) | `_archive/` 이동 | `find . -name "*.bak"` → 0 |
| 하드코딩 경로 인벤토리 (40+) | `data/hardcoded_paths.csv` | 경로 목록 확보 |
| `day_cycle.sh` 행위 명세화 (455줄) | `data/day_cycle_behavior.md` | 상태 전이·예산 로직 명세 |
| 골든마스터 캡처 (DB 상태 + checkpoint + 이미지) | `data/golden_master.json` | `SELECT pipeline_state, COUNT(*) FROM turns GROUP BY ...` |
| **LLM 응답 캡처 (record/replay 하네스)** | `tests/fixtures/llm_recordings/` | 5개 핵심 경로 LLM 응답 fixture 확보 |

**완료**: 골든마스터 + record/replay 하네스 (구현 + 캡처). **LLM 캡처는 여기서 반드시 진행** (Phase 3 이후엔 구 코드가 사라짐).

### Phase 0: 기반 구축 + 특성화 테스트 (Week 1-2) ✅ 완료 (2026-09-21)

**상태:** complete · **검증:** `ruff` pass · `mypy` pass · `pytest tests/unit` 49 passed · `lint-imports` 4 kept / 0 broken (위반 주입으로 강제 검증)

**완료 항목:**
- ✅ `pyproject.toml` 생성 + 설치 가능 (`pip install -e .`)
- ✅ `src/devforge/` 골격 (adapters, application, cli, core, domain, pipeline_stages, ports)
- ✅ `core/config.py` ConfigRegistry + 5-source merge (secrets/providers/runtime/system/state)
- ✅ `core/logging.py` structlog + JSON, single-emission
- ✅ `core/paths.py` Paths SSOT (DATA_DIR/SERVER_DIR/CONFIG_DIR)
- ✅ `core/database.py` SQLAlchemy 2.0 async DatabaseGateway
- ✅ `core/exceptions.py` DevForgeError hierarchy
- ✅ `ports/extract.py` LLMPort
- ✅ `application/extract_pipeline.py`
- ✅ `domain/` 서브디렉토리: `model_management/`, `pipeline/`, `turn_collection/`, `watchdog/`
- ✅ `alembic/` (env + initial + fix_initial_schema)
- ✅ `import-linter` 4 contracts (layering / hexagonal / domain-subpackage-independence / domain-agnostic-of-adapters)
- ✅ 특성화 테스트 5종: `tests/characterization/test_{day_cycle,check_all_llm,call_llm,watchdog,text_clean}.py`

> 상세: `docs/refactoring/REFACTORING_STATUS.yaml` `phase_0`, `docs/refactoring/phase0-work-log.md`.

---

| 계획 주차 | 실제 | 작업 | 검증 |
|------|------|------|------|
| Week 1 | 2026-09-13 ~ **09-21** | `pyproject.toml` + `src/devforge/` 골격, `ConfigRegistry`(5-source), `Paths`, `DatabaseGateway`+Alembic, structlog, `import-linter` | `pip install -e .`, `alembic upgrade head`, `lint-imports` 4 KEPT |
| Week 2 | 2026-09-13 ~ **09-21** (병행) | 특성화 테스트 5종(`day_cycle`·`check_all_llm`·`call_llm`·`watchdog`·`text_clean`) | `tests/characterization/` 5/5 |

**진입**: Phase −1 완료 (골든마스터 + LLM 캡처 필수) — 충족  
**완료**: `devforge --help` 동작, 특성화 테스트 5/5 통과 (계획 09-27 대비 **09-21 완료**)

### Phase 1: 추론 컨테이너 도메인화 + LLM Provider 포트 (Week 3-4) ✅ 완료 (2026-09-22)

> 가이드: `docs/plans/phase1-plan.md`. 산출물: `ports/container.py`·`ports/extract.py`(LLMPort), `adapters/driven/llm/local_adapter.py`, `domain/model_management/`, `application/orchestrator.py`(골격), `core/paths.py`.
> **실제**: 2026-09-21 ~ 09-22 (계획 Week 3-4 대비 단축).

| 주차 | 작업 | 산출물 | 검증 |
|------|------|--------|------|
| **Week 3** | `InferenceContainerManager` 포트 + `SubprocessImpl` | `ports/container.py` | 컨테이너 기동/정지/헬스체크 성공 |
| | `ModelRegistry` 도메인화 | `domain/model_management/registry.py` | `MODEL_METADATA` 조회 성공 |
| | **`LLMPort` 포트 정의** + `LocalLLMAdapter` 구현 | `ports/extract.py` + `adapters/driven/llm/local_adapter.py` | 로컬 포트 호출 성공 (DI 통해 주입) |
| | `cli.py` 내 inference 서브커맨드 이전 | `adapters/driving/cli_cmds/inference.py` | `devforge inference switch day` 동작 |
| **Week 4** | `PipelineOrchestrator` 스케치 (BudgetManager 포함) | `application/orchestrator.py` | `budget_gate()` 동작 |
| | 하드코딩 경로 40곳 검증 | `core/paths.py` | `Paths.data_dir` 오버라이드 가능 |
| | Phase 1.5 ADR 작성 | `docs/adr/0003-shadow-db.md` | 스키마 설계 결정문서 |

### Phase 1.5: 섀도 DB + Replay 하네스 (Week 4.5, 2일) ✅ 완료

> `devforge_shadow` 스키마 라이브 확인(`information_schema`), `scripts/shadow_diff.py`(diff=0 자체검증), `tests/fixtures/replay_harness.py` 존재. 잔여 2주 diff=0 게이트는 리팩터드 파이프라인(Phase 3)과 함께 수행.
> **실제**: ~2026-09-20 ~ 09-22.

| 작업 | 산출물 | 검증 |
|------|--------|------|
| 섀도 DB 스키마 설계 (독립 스키마, Alembic 동일 리비전) | `docs/adr/0003-shadow-db.md` | DDL 검토 |
| `turns_shadow` 테이블 생성 (읽기 전용 뷰) | `sql/shadow_schema.sql` | `SELECT COUNT(*) FROM turns_shadow` |
| replay 하네스 검증 (fixture → API 호출 대체) | `tests/fixtures/replay_harness.py` | 섀도 day_cycle이 replay 사용 |

**목적**: Phase 3의 구/신 비교가 **같은 DB에서 경합하지 않도록** + **LLM 응답 비재현성 해결**

### Phase 2: Watchdog 도메인화 + IssueCollector (Week 5) 🟡 Phase 2.5 shadow-run 진행 중

> **코드 완료**: `docs/plans/phase2-detailed-guide-v2.md` v2.1 **COMPLETE** (18 tasks A1–E3).
> **구현**: `domain/watchdog/{monitoring,orchestration,recovery}`, `adapters/driven/{health,container,recovery,notification}`, `adapters/driven/storage/{incident_pg,state_json,heartbeat_pg}`, `ports/{health_check,heartbeat,incident_repository,notification,recovery,state_persistence,types}`, `application/watchdog_service.py`.
> **현재**: legacy `devforge-watchdog.service` + `devforge-watchdog-v2.service` 동시 active, 24h shadow-run 대조 데이터 수집 중(recovery off). 컷오버는 Phase 2.9. 상세 `docs/plans/watchdog-standard-compliance.md`(정본; 원안 `_archive/plans/phase2-gate4-cutover-plan.md`).
> **실제**: 2026-09-22 코드 완료 → **09-23 shadow-run**. 계획(Week 5, 10 tasks) 대비 **스코프 확장**(18 tasks + Gate 1~4). 잔여: `IssueCollector`, MCP `watchdog_*` 분리.

| 작업 | 산출물 | 검증 |
|------|--------|------|
| `WatchdogOrchestrator` 클래스화 | `domain/watchdog/orchestrator.py` | 60초 루프 정상 동작 |
| `ComponentTracker` + `GraduatedRecovery` | `domain/watchdog/recovery.py` | 백오프/서킷브레이커 보존 |
| `IssueCollector` 구현 | `application/issue_collector.py` + `collected_issues` 테이블 | 자동 수집 트리거 동작 |
| MCP 도구 `watchdog_*` 분리 | `adapters/driving/mcp/tools/watchdog/` | `devforge watchdog status` 동작 |

### Phase 3: 파이프라인 도메인화 + 오케스트레이터 (Week 6-7) ⬜ 계획 확정 (D6=A)

> 가이드: `docs/plans/phase3-plan.md` (approved). 범위: devforge는 **embed** 단계 소유(`enriched → embedded`), shadow diff=0 + 2주 병렬 후 컷오버.
> **재기준(2026-09-23)**: 미착수. Phase 2.9 컷오버(또는 병행) 이후 착수. 계획 Week 6-7은 실제 주차가 아니라 의존성 기준.

| 주차 | 작업 | 산출물 |
|------|------|--------|
| **Week 6** | 파이프라인 단계별 모듈화 | `domain/pipeline/stages/{extract,enrich,embed,review}.py` |
| | `PipelineOrchestrator` 구현 (BudgetManager 포함) | `application/orchestrator.py` |
| | `day_cycle.sh` → `PipelineOrchestrator.run_full_cycle()` | `application/day_cycle.py` |
| | LLM Provider DI (Track A: Local) | `pipeline_stages/{extract,enrich}.py`에서 `self.llm.chat()` 호출 |
| **Week 7** | MCP 도구 `pipeline_*` 7개 | `adapters/driving/mcp/tools/pipeline/` |
| | orchestrate/status/resume/budget_gate | `adapters/driving/mcp/tools/orchestration/` |
| | `day_cycle.sh` → `devforge pipeline orchestrate` 래퍼 | `scripts/day_cycle.sh` (10줄) |

### Phase 3.5: 병렬 검증 (Week 8-9, **2주 확보**) ⬜ 재기준

> **재기준(2026-09-23)**: Phase 3 완료 후 2주 대조 시작. 주차(Week 8-9)는 계획 표기이며 실제 일정은 Phase 3 착수 시점 기준으로 산정.

| 작업 | 검증 |
|------|------|
| **섀도 day_cycle 시작** (devforge_shadow DB + replay) | 프로덕션 DB에 쓰지 않음 |
| **구/신 상태 대조 (2주, 최소 14 사이클)** | - 결정론적 단계: diff = 0<br>- 확률적 단계: 분포 검정 (p < 0.05) |
| Track A LLM Provider 검증 (Local + replay) | 응답 시간 < 10s, 품질 동일 (fixture 대조) |
| 롤백 테스트 (Quadlet digest 고정) | **5분 이내** 롤백 성공 |

**Week 8-9 할당 이유**: day_cycle은 일 1회이므로 2주야 `n ≥ 14` 샘플 확보 가능. 1주(Review에서 지적)면 n≈7으로 통계 검정 불가.


---

> **부록**: 실행 계획 Phase 4~8 및 업계 표준 비교는 [REFACTORING_PLAN-appendix.md](./REFACTORING_PLAN-appendix.md) 참조.
