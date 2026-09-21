# Phase 1 구현 가이드 — 추론 컨테이너 도메인화 + LLM Provider 포트

**Status:** active · **Date:** 2026-09-21 (rev.2) · **Owner:** devforge
**대상:** AI 에이전트 또는 개발자 · **난이도:** High · **예상:** 2–3일 (Week 3–4)
**정본 계획:** `REFACTORING_PLAN.md` Phase 1 / `REFACTORING_STATUS.yaml`
**선행:** Phase 0 complete (2026-09-21).

> **rev.2 반영:** ① 가이드 SSOT 규칙(최종 상태 기준, §1.1) ② 선행 정비(유닛 동기화·auto-sync, §0.1)
> ③ `CORE-DB-UNWIRED` 프레이밍 정정 ④ Phase 1.5 실측 반영.

---

## 0. 범위와 현재 상태

Phase 1 목표: **추론 컨테이너 제어와 LLM 호출을 포트/어댑터로 분리**하고, 파이프라인
오케스트레이터의 골격을 세운다. 라이브 프로덕션(`scripts/`)은 **그대로 구동**하며
devforge 쪽에 병렬 구현한다(비파괴).

### 이미 완료 (재작업 금지)
| 항목 | 파일 | 비고 |
|------|------|------|
| LLMPort 인터페이스 | `ports/extract.py` | chat/extract_facts/verify_claim/enrich_fact/rerank + ExtractPort 등 |
| LocalLLMAdapter | `adapters/driven/llm/local_adapter.py` | llama.cpp HTTP + MODEL_REGISTRY 사본 |
| inference CLI(부분) | `adapters/driving/cli_cmds/inference.py` | switch/status/ensure — 현재 socket/subprocess 직접 |
| ORM 모델 | `domain/models.py` | schema SSOT |
| Alembic | `alembic/` | initial + fix_initial_schema |
| Shadow DB(Phase 1.5) | ADR-0003 Applied, `devforge_shadow`, `scripts/shadow_diff.py`, `tests/fixtures/replay_harness.py` | 잔여는 Phase 3 이후 2주 diff 게이트 |

### 남은 작업
| # | 작업 | 산출물 | 상태 |
|---|------|--------|------|
| 1 | 추론 컨테이너 포트 정의 | `ports/container.py` | ⬜ |
| 2 | 모델 레지스트리 도메인화 | `domain/model_management/registry.py` | ⬜ |
| 3 | podman 어댑터(SubprocessImpl) | `adapters/driven/container/podman_adapter.py` | ⬜ |
| 4 | inference CLI 포트 기반 전환 | `adapters/driving/cli_cmds/inference.py` | ⬜ |
| 5 | PipelineOrchestrator 골격(+BudgetManager) | `application/orchestrator.py` | ⬜ |
| 6 | 경로 추상화 검증 | `core/paths.py` 점검 + 테스트 | ⬜ |

### 선행 판단 — `core/database.py` (CORE-DB-UNWIRED-2026-09-21)

검토문은 이를 "ModelRegistry가 SQLAlchemy를 쓰는가"로 연결했으나 **부정확**하다.
`ModelRegistry`는 정적 메타데이터라 DB가 필요 없다. 실제 쟁점은 **DB 게이트웨이 중복**이다.

- `core/database.py` (QueuePool primitive) — 현재 테스트만 참조(미배선).
- `adapters/driven/storage/database_gateway.py` (NullPool + connect_args + FastAPI `get_db`)
  — 실사용(MCP/FastAPI/extract_adapter).

두 구현은 풀 전략이 달라 의도적 분리이므로, 결정은 다음 중 하나:
- **A. 통합(권장)**: 어댑터가 `core.database`의 엔진/세션 팩토리를 재사용 → 단일 SSOT.
- **B. 제거**: `core/database.py` 삭제, DB는 어댑터 단독 소유(계획 §3.2 수정 필요).
- **C. 유지**: 역할만 문서화 — 미배선 dead code 위험 잔존.

> 어느 경우든 Task 1~3과 독립적이며 Phase 1 착수를 막지 않는다(blocking 아님).

### 0.1 선행 정비 — 유닛 동기화 · auto-sync (완료 2026-09-21)

Stage 3 전환으로 유닛 수정 빈도가 늘어, 배포 전에 정비했다.

- **유닛 미러**: `containers/systemd/`(quadlet) + `systemd/user/`(hand-written).
  실제 배포 위치는 `~/.config/...`. **드리프트 체크/배포 스크립트 신규**:
  ```bash
  scripts/deploy/sync-units.sh --check   # 드리프트만 보고(exit 1=drift)
  scripts/deploy/sync-units.sh           # repo -> live 복사 + daemon-reload
  ```
  **규칙: repo 미러가 SSOT.** live를 직접 고치지 말고 미러를 고친 뒤 sync 한다.
  (선택) `devforge-daily-structure`에 `sync-units.sh --check`를 알림용으로 추가.
- **auto-sync 제외 확대**: `scripts/system_sync.sh`가 이제 `src tests scripts pyproject.toml
  docs '*.md'`를 커밋에서 제외한다. 근거: `bd41445`(소스 흡수), `0dacca5`(문서 흡수 — INDEX 수정과
  문서 이동이 `auto: sync`로 흡수됨). 즉 "문서 흡수 없음"은 사실이 아니며 위험은 현실화된 적 있다.

---

## 1. 아키텍처 제약 (import-linter가 강제)

```
layering (높음→낮음, 높은 층이 낮은 층을 import 가능):
  application > pipeline_stages > adapters > domain > core > ports
```

- **역방향 import 금지**: `adapters`가 `application`을 import하면 계약 위반.
- **composition root 규칙**: `devforge/cli.py`(계층 밖)와 MCP의 `set_pipeline_factory()`
  주입만이 application을 조립할 수 있다. driving adapter 내부에서 application 직접 import 금지.
- **domain은 adapters/application을 모른다.** `domain/model_management`는 순수 타입만.
- **드라이버 의존 금지**: domain/core/ports는 `podman`, `urllib` 등 외부 기술을 모른다.
- 검증: `lint-imports` (4 contracts KEPT) — 편집 전후 필수.

### 1.1 가이드 SSOT 규칙 (rev.2 핵심)

**이 가이드의 예제 코드는 최종 상태(final state) 기준이다.** 즉 devforge 패키지 import를
전제로 작성한다:

```python
from devforge.core.database import DatabaseGateway
from devforge.ports.container import InferenceContainerManager
from devforge.domain.model_management.registry import ModelRegistry
```

- 근거: **가이드가 구현의 SSOT**이며, 구현이 가이드를 따른다(중간 상태를 가이드에 박지 않음).
- ⚠️ **Phase 1 초기에는 위 import가 실패할 수 있다(정상).** devforge는 아직 프로덕션
  미배선이고, 모듈이 순차 추가되기 때문이다. 구현은 해당 모듈을 만든 뒤 그 import를
  사용하도록 점진 전환한다. 중간 단계의 import 에러는 결함이 아니다.
- 레거시(`scripts/lib/*`)는 **계속 구동**하며, 전환은 devforge 쪽에서 병렬로 진행한다.

---

## 2. Task 1 — `ports/container.py`

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
    def health(self, port: int, timeout: int = 3) -> bool: ...

    @abstractmethod
    def model_identity(self, port: int, model_key: str) -> bool: ...

    @abstractmethod
    def ensure_model(self, model_key: str, skip_if_healthy: bool = False) -> bool: ...

    @abstractmethod
    def switch_mode(self, mode: str, port: int,
                    model_key: Optional[str] = None) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...
```

**레거시 대응표** (동작 보존 기준):
| 포트 메서드 | 레거시 원본 |
|-------------|-------------|
| `health` | `lib/pod_manager/__init__.py:wait_health`, `container.py:_check_container_health` |
| `model_identity` | `container.py:_check_model_identity` / `_get_model_fingerprint` |
| `ensure_model` | `__init__.py:ensure_model` (skip_if_healthy 포함) |
| `switch_mode` | `cli.py:_switch_mode` + `container.py:_write_mode_env` |
| `stop` | `container.py:_podman_stop_inference` |

**검증:** `python3 -c "from devforge.ports.container import InferenceContainerManager"`,
`lint-imports` (ports는 아무것도 import하지 않음).

---

## 3. Task 2 — `domain/model_management/registry.py`

**목표:** `MODEL_METADATA`를 타입 있는 도메인 모델로 승격. **순수(Pure)** — I/O 없음.

```python
# src/devforge/domain/model_management/registry.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping


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
    def get(self, key: str) -> ModelMetadata: ...          # KeyError -> 명확한 도메인 예외
    def by_mode(self, mode: str) -> list[ModelMetadata]: ...
```

- **SSOT = devforge (최종 상태, §1.1 규칙).** 매핑은 `domain/model_management/metadata.py`에
  두고, 레거시 `scripts/lib/model_registry.py`는 전환기에 `from devforge...` 위임으로 얇게 만든다.
  전환 전까지는 **패리티 테스트**로 devforge 매핑 vs 레거시 `MODEL_METADATA` 키/필드 일치를 강제한다.
  (레거시 위임은 devforge가 런타임 import 가능해진 뒤 적용 — 그 전엔 중복+패리티 테스트.)

---

## 4. Task 3 — `adapters/driven/container/podman_adapter.py`

**목표:** `InferenceContainerManager`의 실제 구현(subprocess/podman).

```python
class PodmanInferenceAdapter(InferenceContainerManager):
    def __init__(self, registry: ModelRegistry, mode_env_path, *, dry_run: bool = False): ...
    # health/model_identity/ensure_model/switch_mode/stop 구현
```

- 컨테이너: `devforge-inference`(`llama.cpp:server`), 포트 8080–8084.
- 모드 env: `core.paths.Paths.current_mode_env`(= `/opt/ai_data/scripts/current-mode-inference.env`).
- **레거시 동작 보존**: health 대기 600s, probe 600s, 모델 신원 재시도(최대 5회, backoff),
  실패 시 GC+재시도. bare `except` 금지, Tenacity/수동 재시도.
- `dry_run=True`면 podman 미호출.

**검증:** subprocess mock 단위 테스트(정상/타임아웃/신원 불일치/dry_run).

---

## 5. Task 4 — inference CLI 포트 기반 전환

**대상:** `adapters/driving/cli_cmds/inference.py`(현재 socket/subprocess 직접).

- composition root(`devforge/cli.py`)에서 `PodmanInferenceAdapter` + `ModelRegistry` 생성 후
  `inference_cmds.init(manager, registry)`로 주입(전역 setter) — driving이 driven 생성자를 직접
  import하지 않아도 됨. (동일 계층 import도 계약 위반은 아니나 주입 권장.)
- 명령 시그니처 유지: `devforge inference switch <mode>`, `status`, `ensure <key>`.
- `switch`→`switch_mode()`, `ensure`→`ensure_model()`, `status`→`ModelRegistry`+`Paths` 조회.

**검증:** 기존 출력 필드 동일(문자열 회귀), `devforge inference status` 스모크.

---

## 6. Task 5 — `application/orchestrator.py` (골격만)

```python
@dataclass
class Budget:
    limit_sec: int
    started_at: datetime
    def remaining(self) -> float: ...
    def expired(self) -> bool: ...
    def gate(self) -> None: ...   # 초과 시 PipelineBudgetExceeded

class PipelineOrchestrator:
    def __init__(self, stages: list, budget: Budget): ...
    async def run(self) -> list[str]: ...
```

- `datetime.now(tz)` 사용(`utcnow()` 금지), tz=Asia/Seoul.
- **단계 구현은 Phase 3.** 여기서는 예산/게이트 골격 + 단위 테스트만.

---

## 7. Task 6 — 경로 검증 + Phase 1.5 확인

- `core/paths.py` 경로가 실제와 일치하는지 `data/hardcoded_paths.csv`와 대조, 대체 가능한
  잔존 하드코딩 목록화(전면 치환은 Phase 3).
- **Phase 1.5 실측: 이미 대부분 완료.** ADR-0003=Applied, `devforge_shadow`(turns_shadow+
  review_facts_shadow), `scripts/shadow_diff.py`(diff=0 self-verified), `replay_harness.py` 존재.
  잔여는 Phase 3 이후 2주 diff 게이트뿐 — Phase 1에서 새로 만들 것 없음.

---

## 8. 테스트 전략

- **단위(`tests/unit/`)**: `ModelRegistry`(get/by_mode/오류), `PodmanInferenceAdapter`
  (subprocess mock), `Budget`/`PipelineOrchestrator`.
- **패리티**: devforge `ModelRegistry` vs 레거시 `MODEL_METADATA`(키/필드 일치).
- **특성화(`tests/characterization/`)**: Phase 0 자산 재사용. 필요 시 실제
  `switch day→night→day` 1회를 운영 창 합의 후 수동 검증.
- 금지: 라이브 LLM/포트 의존 기본 테스트(integration 마커로 분리).

---

## 9. 검증 체크리스트

```bash
cd /opt/projects/server
python3 -m ruff check src/ tests/      # 0 findings
python3 -m mypy src/devforge/           # strict, 0 errors
lint-imports                            # 4 contracts KEPT
python3 -m pytest tests/unit/ tests/characterization/ -q   # green
scripts/deploy/sync-units.sh --check    # units no drift
```
- [ ] `ports/container.py`가 domain/core/adapters를 import하지 않음
- [ ] `domain/model_management`가 adapters/application을 import하지 않음
- [ ] `devforge inference status` 출력이 현행과 동일
- [ ] 라이브 `scripts/` 파이프라인 무영향

---

## 10. Git 커밋 (원자적 단위)

```bash
git commit -m "feat(phase-1): add InferenceContainerManager port"
git commit -m "feat(phase-1): add ModelRegistry domain model (+ parity test)"
git commit -m "feat(phase-1): add PodmanInferenceAdapter (SubprocessImpl)"
git commit -m "refactor(phase-1): inference CLI uses container port"
git commit -m "feat(phase-1): PipelineOrchestrator + BudgetManager sketch"
```
- 커밋별 `lint-imports`/`pytest` 통과 유지. §1.1 SSOT 규칙과 중간 import 예외를 본문에 기록.

---

## 11. 리스크 및 완화

| 리스크 | 완화 |
|--------|------|
| 중간 단계 devforge import 실패 | §1.1 문서화(정상), 모듈 추가 후 점진 전환 |
| 레거시 MODEL_METADATA 드리프트 | 패리티 테스트로 불일치 차단 |
| 유닛 미러 vs live 드리프트 | `sync-units.sh --check` (+ 선택: daily 타이머 알림) |
| auto-sync가 수동 커밋 흡수 | `docs '*.md'` 제외 적용(0dacca5 재발 방지) |
| 컨테이너 재시작 지연(~70s) | dry_run/mock 테스트 + 실제 1회 수동 검증 |
| 과설계 | 오케스트레이터는 골격만, 단계는 Phase 3 |

---

## 12. 다음 단계
- **Phase 2:** Watchdog 도메인화 + IssueCollector.
- **Phase 3:** 파이프라인 단계 모듈화 + `day_cycle.sh` → `PipelineOrchestrator`.
- **별도(Phase 2 이후) 보안:** 세션 검증 강화 — 쿠키 서명(itsdangerous) 또는 서버측 저장소.
  현황: `or True` 제거 완료(e1512f7)했으나 쿠키 서명/서버측 세션은 미구현(위조 가능성 잔존).
