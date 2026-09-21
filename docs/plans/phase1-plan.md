# Phase 1 구현 가이드 — 추론 컨테이너 도메인화 + LLM Provider 포트

**Status:** active · **Date:** 2026-09-21 · **Owner:** devforge
**대상:** AI 에이전트 또는 개발자 · **난이도:** High · **예상:** 2–3일 (Week 3–4)
**정본 계획:** `REFACTORING_PLAN.md` Phase 1 / `REFACTORING_STATUS.yaml`
**선행:** Phase 0 complete (2026-09-21).
**선행 판단:** `CORE-DB-UNWIRED-2026-09-21` — 단, 이는 **DatabaseGateway 중복** 문제이지
`ModelRegistry`와 무관하다(레지스트리는 정적 메타데이터로 SQLAlchemy가 필요 없음).
아래 §3.1의 정확한 판단 기준 참조.

---

## 0. 범위와 현재 상태

Phase 1 목표: **추론 컨테이너 제어와 LLM 호출을 포트/어댑터로 분리**하고, 파이프라인
오케스트레이터의 골격을 세운다. 기존 프로덕션(`scripts/`)은 **그대로 구동**하며
devforge 쪽에 병렬 구현한다(비파괴).

### 이미 완료 (재작업 금지)
| 항목 | 파일 | 비고 |
|------|------|------|
| LLMPort 인터페이스 | `ports/extract.py` | chat/extract_facts/verify_claim/enrich_fact/rerank + ExtractPort/TurnRepository |
| LocalLLMAdapter | `adapters/driven/llm/local_adapter.py` | llama.cpp HTTP, MODEL_REGISTRY 사본 보유 |
| inference CLI(부분) | `adapters/driving/cli_cmds/inference.py` | switch/status/ensure — 단, 포트 미사용(socket 직접) |
| ORM 모델 | `domain/models.py` | schema SSOT |
| Alembic | `alembic/` | initial + fix_initial_schema |
| Shadow DB ADR | `docs/adr/0003-shadow-db.md` | Phase 1.5 ADR 완료 |

### 남은 작업
| # | 작업 | 산출물 |
|---|------|--------|
| 1 | 추론 컨테이너 포트 정의 | `ports/container.py` |
| 2 | 모델 레지스트리 도메인화 | `domain/model_management/registry.py` |
| 3 | podman 어댑터(SubprocessImpl) | `adapters/driven/container/podman_adapter.py` |
| 4 | inference CLI를 포트 기반으로 전환 | `adapters/driving/cli_cmds/inference.py` |
| 5 | PipelineOrchestrator 골격(+BudgetManager) | `application/orchestrator.py` |
| 6 | 경로 추상화 검증(하드코딩 잔존) | `core/paths.py` 점검 + 테스트 |

### 선행 판단 — `core/database.py` (CORE-DB-UNWIRED-2026-09-21)

Phase 0 검토문은 이 이슈를 "ModelRegistry가 SQLAlchemy를 쓰는가"로 연결했으나 **부정확**하다.
`ModelRegistry`는 정적 메타데이터라 DB가 필요 없다. 실제 쟁점은 **DB 게이트웨이 중복**이다.

- `core/database.py` (QueuePool primitive) — 현재 테스트만 참조(미배선).
- `adapters/driven/storage/database_gateway.py` (NullPool + connect_args + FastAPI `get_db`)
  — 실사용(MCP/FastAPI/extract_adapter).

두 구현은 풀 전략이 달라 의도적 분리이므로, 결정은 다음 중 하나:
- **A. 통합(권장)**: 어댑터가 `core.database`의 엔진/세션 팩토리를 재사용 → 단일 SSOT.
  core는 low-level primitive, 서비스 게이트웨이는 어댑터가 담당. 중복 제거.
- **B. 제거**: `core/database.py` 삭제, DB는 어댑터 단독 소유. (계획 §3.2의
  `core/database.py` 항목과 충돌 → 계획도 수정 필요)
- **C. 유지**: 두 계층으로 두되 역할을 문서화(현행) — 미배선 dead code 위험 잔존.

> 어느 경우든 Task 1~3과 독립적이며 Phase 1 착수를 막지 않는다(blocking 아님).

---

## 1. 아키텍처 제약 (반드시 준수 — import-linter가 강제)

```
layering (높음→낮음, 높은 층이 낮은 층을 import 가능):
  application > pipeline_stages > adapters > domain > core > ports
```

- **역방향 import 금지**: `adapters`가 `application`을 import하면 계약 위반.
- **composition root 규칙**: CLI 진입점 `devforge/cli.py`(계층 밖)와 MCP의
  `set_pipeline_factory()` 주입 패턴만이 application을 조립할 수 있다.
  driving adapter 내부에서 application을 직접 import하지 말 것(기존 `api/app.py`의
  함수 내 import도 신규 코드에서는 피한다).
- **domain은 adapters/application을 모른다**. `domain/model_management`는 순수 타입만.
- **드라이버 의존 금지**: domain/core/ports는 `podman`, `urllib` 등 외부 기술을 모른다.
- 검증: `lint-imports` (4 contracts KEPT) — 편집 전후로 필수 실행.

---

## 2. Task 1 — `ports/container.py` (추론 컨테이너 포트)

**목표:** 컨테이너 수명주기를 인터페이스로 고정. 구현은 어댑터가 담당.

```python
# src/devforge/ports/container.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ContainerHealth:
    port: int
    ok: bool
    model_key: Optional[str] = None
    detail: str = ""


class InferenceContainerManager(ABC):
    """추론 컨테이너(llama.cpp) 수명주기 + 모델 신원 확인."""

    @abstractmethod
    def health(self, port: int, timeout: int = 3) -> bool:
        """HTTP /health 가 200이면 True."""

    @abstractmethod
    def model_identity(self, port: int, model_key: str) -> bool:
        """현재 서빙 중인 모델이 요청한 model_key와 일치하는지(핑거프린트)."""

    @abstractmethod
    def ensure_model(self, model_key: str, skip_if_healthy: bool = False) -> bool:
        """모델이 준비되도록 보장(필요 시 재시작). 멱등."""

    @abstractmethod
    def switch_mode(self, mode: str, port: int,
                    model_key: Optional[str] = None) -> bool:
        """모드(day/night/review/verify) 전환 + 모드 env 기록 + 재시작."""

    @abstractmethod
    def stop(self) -> None:
        """추론 컨테이너 정지(메모리 회수)."""
```

**레거시 대응표** (구현 시 동작 보존 기준):
| 포트 메서드 | 레거시 원본 |
|-------------|-------------|
| `health` | `lib/pod_manager/__init__.py:wait_health`, `container.py:_check_container_health` |
| `model_identity` | `container.py:_check_model_identity` / `_get_model_fingerprint` |
| `ensure_model` | `__init__.py:ensure_model` (skip_if_healthy 포함) |
| `switch_mode` | `cli.py:_switch_mode` + `container.py:_write_mode_env` |
| `stop` | `container.py:_podman_stop_inference` |

---

## 3. Task 2 — `domain/model_management/registry.py` (ModelRegistry)

**목표:** `MODEL_METADATA`를 타입 있는 도메인 모델로 승격. **순수(Pure)** — I/O 없음.

```python
# src/devforge/domain/model_management/registry.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping, Optional


@dataclass(frozen=True)
class ModelMetadata:
    key: str
    file: str
    port: int
    mode: str            # day|night|embed|rerank|review|verify
    model_name: str
    ctx: int = 8192
    threads: int = 4


class ModelRegistry:
    def __init__(self, metadata: Mapping[str, ModelMetadata]) -> None: ...
    def get(self, key: str) -> ModelMetadata: ...          # KeyError → 명확한 예외
    def by_mode(self, mode: str) -> list[ModelMetadata]: ...
```

### ⚠️ SSOT 결정 (반드시 사용자 확인)
`MODEL_METADATA`는 현재 `scripts/lib/model_registry.py`가 SSOT이며 `pod_manager`가
사용 중이다. 도메인화 방식은 둘 중 하나:

- **Option A (권장, 비파괴):** devforge를 SSOT로 승격 →
  `domain/model_management/metadata.py`에 매핑을 두고, 레거시
  `scripts/lib/model_registry.py`는 `from devforge...` import로 얇게 위임.
  전제: 런타임에 `devforge`가 import 가능(`pip install -e .`)해야 함 → **확인 필요**.
- **Option B (보수적):** Phase 1에서는 도메인 타입만 정의하고 SSOT는 레거시 유지.
  어댑터/composition root가 레거시 dict를 로드해 `ModelRegistry`에 주입.
  + **패리티 테스트**로 두 사본 불일치를 차단.

> 권장: 우선 **Option B**로 안전하게 시작하고, devforge가 런타임 import 가능함이
> 확인되면 Option A로 승격(별도 커밋). 판단 근거를 커밋 메시지에 남긴다.

---

## 4. Task 3 — `adapters/driven/container/podman_adapter.py`

**목표:** `InferenceContainerManager`의 실제 구현. podman CLI/subprocess 사용.

```python
# src/devforge/adapters/driven/container/podman_adapter.py
from __future__ import annotations
from devforge.ports.container import InferenceContainerManager
from devforge.core.logging import get_logger

class PodmanInferenceAdapter(InferenceContainerManager):
    def __init__(self, registry, mode_env_path, *, dry_run: bool = False): ...
    def health(self, port, timeout=3) -> bool: ...
    def model_identity(self, port, model_key) -> bool: ...
    def ensure_model(self, model_key, skip_if_healthy=False) -> bool: ...
    def switch_mode(self, mode, port, model_key=None) -> bool: ...
    def stop(self) -> None: ...
```

- 컨테이너명/이미지: `devforge-inference` (`llama.cpp:server`), 포트 8080–8084.
- 모드 env: `core/paths.Paths.current_mode_env` (= `/opt/ai_data/scripts/current-mode-inference.env`).
- **재시도/백오프**: 레거시 `_start_and_wait`(health 600s, probe 600s)와 모델 신원
  재시도(최대 5회, backoff) 동작을 보존. bare `except` 금지.
- `dry_run=True`면 실제 podman 호출 없이 로그만(테스트/시뮬레이션 용).

---

## 5. Task 4 — inference CLI를 포트 기반으로 전환

**대상:** `adapters/driving/cli_cmds/inference.py` (현재 socket/subprocess 직접 사용).

- 주입 방식: `devforge/cli.py`(composition root)에서 `PodmanInferenceAdapter` +
  `ModelRegistry`를 만들고, `inference_cmds.init(manager, registry)`로 주입(전역 setter).
  → driving adapter가 driven adapter 생성자를 직접 import하지 않아도 됨(선택).
  단, 동일 계층(adapters) import는 허용되므로 직접 import도 계약 위반은 아니다.
- 기존 명령 시그니처 유지: `devforge inference switch <mode>`, `status`, `ensure <key>`.
- `switch`: 포트 `switch_mode()` 사용. `ensure`: 포트 `ensure_model()` 사용.
- `status`: `ModelRegistry` + `Paths`에서 조회(현행 출력 필드 유지).

---

## 6. Task 5 — `application/orchestrator.py` (골격 + BudgetManager)

**목표:** Phase 3에서 완성할 오케스트레이터의 **스케치**만. 도메인 로직은 넣지 않는다.

```python
# src/devforge/application/orchestrator.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")

@dataclass
class Budget:
    limit_sec: int
    started_at: datetime

    def remaining(self) -> float: ...
    def expired(self) -> bool: ...
    def gate(self) -> None:
        """예산 초과 시 PipelineBudgetExceeded 발생."""

class PipelineOrchestrator:
    """파이프라인 단계 실행 골격 — Phase 3에서 day_cycle.sh 대체."""
    def __init__(self, stages: list, budget: Budget): ...
    async def run(self) -> list[str]: ...
```

- `utcnow()` 금지(프로젝트 규칙) → `datetime.now(tz)` 사용.
- 예산/게이트 단위 테스트 필수(Task 6과 함께).

---

## 7. Task 6 — 경로 추상화 검증 + Phase 1.5 확인

- `core/paths.py`의 모든 경로가 실제 사용처와 일치하는지 점검
  (`data/hardcoded_paths.csv` 인벤토리 대조). `Paths`로 대체 가능한 잔존 하드코딩을
  목록화(전면 치환은 Phase 3에서).
- **Phase 1.5 상태(실측 2026-09-21): 이미 대부분 완료.**
  - `docs/adr/0003-shadow-db.md` = **Applied**(frozen 아님).
  - 실제 자산 존재: `devforge_shadow` 스키마(`turns_shadow` 뷰 +
    `review_facts_shadow`), `scripts/shadow_diff.py`(self-verified diff=0),
    `tests/fixtures/replay_harness.py`.
  - **잔여**: 리팩터드 파이프라인 구현 후 실제 shadow run 2주 diff=0 게이트 —
    Phase 3 이후로 이월. Phase 1에서 새로 만들 것은 없음.
  > 검토문의 "Week 4 0.5일(Shadow DB + 체크포인트)" 및 본 계획의 "Week 4.5 2일"은
  > 모두 현행 대비 과대 추정(이미 완료분 반영). REFACTORING_STATUS 반영 권장.
- `application/orchestrator.py`의 예산 게이트 단위 테스트 추가.

---

## 8. 테스트 전략

- **단위(`tests/unit/`)**: `ModelRegistry`(get/by_mode/오류), `PodmanInferenceAdapter`
  (subprocess mock — health true/false, model_identity retry, dry_run),
  `Budget`/`PipelineOrchestrator`(남은 시간/만료/게이트).
- **특성화(`tests/characterization/`)**: Phase 0 자산 재사용. 필요 시
  `regression_test.sh`로 실제 `switch day→night→day` 1회 검증(운영 창 합의 후).
- **패리티(Option B 채택 시)**: devforge `ModelRegistry` vs 레거시 `MODEL_METADATA`
  키/필드 일치 테스트.
- 금지: 라이브 LLM/포트에 의존하는 기본 테스트(integration 마커로 분리).

---

## 9. 검증 체크리스트

```bash
cd /opt/projects/server
python3 -m ruff check src/ tests/           # 0 findings
python3 -m mypy src/devforge/                # strict, 0 errors
lint-imports                                 # 4 contracts KEPT
python3 -m pytest tests/unit/ tests/characterization/ -q   # green
python3 -c "import devforge.ports.container, devforge.domain.model_management.registry"
```
- [ ] `ports/container.py`가 domain/core/adapters를 import하지 않음
- [ ] `domain/model_management`가 adapters/application을 import하지 않음
- [ ] `devforge inference status`가 현행과 동일 출력
- [ ] 라이브 `scripts/` 파이프라인 무영향(devforge는 미배선 유지)

---

## 10. Git 커밋 (작업 단위로 분리)

```bash
git commit -m "feat(phase-1): add InferenceContainerManager port"
git commit -m "feat(phase-1): add ModelRegistry domain model"
git commit -m "feat(phase-1): add PodmanInferenceAdapter (SubprocessImpl)"
git commit -m "refactor(phase-1): inference CLI uses container port"
git commit -m "feat(phase-1): PipelineOrchestrator + BudgetManager sketch"
```
- 커밋별로 `lint-imports`/`pytest` 통과 유지(회귀 없는 원자적 커밋).
- SSOT 결정(A/B)과 예외 처리를 커밋 본문에 기록.

---

## 11. 리스크 및 완화

| 리스크 | 완화 |
|--------|------|
| devforge 미설치 런타임에서 Option A import 실패 | Option B로 시작, 설치 검증 후 승격 |
| 레거시 `scripts/` 동작 변경 | devforge는 병렬 구현, 라이브 미배선 유지 |
| 컨테이너 재시작 지연(ARM 모델 로드 ~70s) | `dry_run`/mock 테스트 + 실제 1회만 수동 검증 |
| 순환 import | ports(최하위)→domain→adapters 순, `lint-imports`로 즉시 확인 |
| 과설계 | 오케스트레이터는 골격만. 단계 구현은 Phase 3 |

---

## 12. 다음 단계
- **Phase 2:** Watchdog 도메인화 + IssueCollector.
- **Phase 3:** 파이프라인 단계 모듈화 + `day_cycle.sh` → `PipelineOrchestrator`.
- 별도 판단: `CORE-DB-UNWIRED-2026-09-21`(core/database.py 배선/제거), 진짜 세션 검증.
