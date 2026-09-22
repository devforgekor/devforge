# Phase 1 구현 가이드 — 추론 컨테이너 도메인화 + LLM Provider 포트

**Status:** active · **Date:** 2026-09-22 (rev.3) · **Owner:** devforge
**대상:** AI 에이전트 또는 개발자 · **난이도:** High · **예상:** 2–3일 (Week 3–4)
**정본 계획:** `REFACTORING_PLAN.md` Phase 1 / `REFACTORING_STATUS.yaml`
**선행 문서:** `docs/plans/python-version-strategy.md`, handover `PY-RUNTIME-SPLIT-2026-09-22`

> **rev.3 반영:** ① **Python 3.12 표준화 완료**(§0.2) ② **우선순위 확정 — Phase 1 먼저,
> 유닛/legacy 이관은 컷오버 이월**(§0.1) ③ 가이드 SSOT 규칙(§1.1) ④ 선행 정비(§0.3)
> ⑤ `CORE-DB-UNWIRED` 프레이밍 정정(§0.4) ⑥ Phase 1.5 실측(§7).

---

## 0. 컨텍스트와 전제

Phase 1 목표: **추론 컨테이너 제어와 LLM 호출을 포트/어댑터로 분리**하고, 파이프라인
오케스트레이터의 골격을 세운다. 라이브 프로덕션(`scripts/`)은 **그대로 구동**하며
devforge 쪽에 병렬 구현한다(비파괴).

### 0.1 이 가이드의 위치 (우선순위 확정, 2026-09-22)

**Phase 1 Task 1을 먼저 진행한다.** 근거(실측):
- 호스트 user unit 중 **`devforge` 패키지를 실행하는 것은 0개** — 전부 legacy
  `scripts/*.py`를 `/usr/bin/python3`(3.9)로 실행. `devforge`는 컨테이너(3.12)+테스트(3.12)에서만 사용.
- 따라서 "devforge 관련 유닛을 3.12로" 전환은 **대상이 없고**, 유닛을 3.12로 바꾸는 것은
  곧 **legacy 스크립트를 3.12에서 실행**하는 일 → **컷오버(Phase A~I)** 로 이월.
- Phase 1은 Step 4/5와 의존관계가 없고, 3.12 표준화로 선행조건이 충족되었다.

### 0.2 환경 / 툴체인 (Python 3.12, 검증 완료)

| 항목 | 값 |
|------|-----|
| devforge 기준 | **Python 3.12** (`python3.12` = 3.12.14, 호스트 설치 완료) |
| devforge 설치 | `python3.12 -m pip install --user -e ".[dev]"` |
| `pyproject.toml` | `requires-python>=3.12`, ruff `py312`, mypy `python_version=3.12` |
| 컨테이너 | devforge-base/fastapi/mcp/worker = 3.12.13 (변경 불필요) |
| legacy `scripts/` / 기본 `python3` | 3.9 유지(당분간) |
| 검증된 게이트(3.12) | pytest 68 passed · ruff pass · mypy 41 files success · lint-imports 4 KEPT |

> 명령은 모두 `python3.12`를 사용한다. `python3`(3.9)는 legacy 전용이다.

### 0.3 선행 완료 (재작업 금지)

| 항목 | 파일 | 비고 |
|------|------|------|
| Phase 0 | — | config/paths/database/exceptions/import-linter/특성화 테스트 5종 |
| Python 3.12 표준화 | `pyproject.toml`, `Dockerfile`, `ci.yml` | §0.2 |
| LLMPort 인터페이스 | `ports/extract.py` | chat/extract_facts/verify_claim/enrich_fact/rerank + ExtractPort 등 |
| LocalLLMAdapter | `adapters/driven/llm/local_adapter.py` | llama.cpp HTTP + MODEL_REGISTRY 사본 |
| inference CLI(부분) | `adapters/driving/cli_cmds/inference.py` | switch/status/ensure — 현재 socket/subprocess 직접 |
| ORM 모델 / Alembic | `domain/models.py`, `alembic/` | schema SSOT |
| Shadow DB(Phase 1.5) | ADR-0003 Applied, `devforge_shadow`, `scripts/shadow_diff.py`, `tests/fixtures/replay_harness.py` | §7 |
| 유닛 동기화 | `scripts/deploy/sync-units.sh` | repo 미러=SSOT, `--check`=드리프트 |
| auto-sync 제외 | `scripts/system_sync.sh` | `src tests scripts pyproject.toml docs '*.md'` 제외 |

### 0.4 선행 판단 — `core/database.py` (CORE-DB-UNWIRED-2026-09-21)

검토문은 이를 "ModelRegistry가 SQLAlchemy를 쓰는가"로 연결했으나 **부정확**하다.
`ModelRegistry`는 정적 메타데이터라 DB가 필요 없다. 실제 쟁점은 **DB 게이트웨이 중복**이다.

- `core/database.py` (QueuePool primitive) — 현재 테스트만 참조(미배선).
- `adapters/driven/storage/database_gateway.py` (NullPool + connect_args + FastAPI `get_db`)
  — 실사용(MCP/FastAPI/extract_adapter).

결정: **A. 통합(권장)** 어댑터가 `core.database`의 엔진/세션 팩토리를 재사용 / **B. 제거** /
**C. 유지**. 어느 경우든 Task 1~3과 독립이며 **blocking 아님**(storage 배선 시점에 확정).

### 0.5 남은 작업

| # | 작업 | 산출물 | 상태 |
|---|------|--------|------|
| 1 | 추론 컨테이너 포트 정의 | `ports/container.py` | ⬜ |
| 2 | 모델 레지스트리 도메인화 | `domain/model_management/registry.py` (+ `metadata.py`) | ⬜ |
| 3 | podman 어댑터(SubprocessImpl) | `adapters/driven/container/podman_adapter.py` | ⬜ |
| 4 | inference CLI 포트 기반 전환 | `adapters/driving/cli_cmds/inference.py` | ⬜ |
| 5 | PipelineOrchestrator 골격(+BudgetManager) | `application/orchestrator.py` | ⬜ |
| 6 | 경로 추상화 검증 | `core/paths.py` 점검 + 테스트 | ⬜ |

---

## 1. 아키텍처 제약 (import-linter가 강제)

```
layering (높음→낮음, 높은 층이 낮은 층을 import 가능):
  application > pipeline_stages > adapters > domain > core > ports
```

- **역방향 import 금지**: `adapters`가 `application`을 import하면 계약 위반.
- **composition root 규칙**: `devforge/cli.py`(계층 밖)와 MCP의 `set_pipeline_factory()`
  주입만이 application을 조립한다. driving adapter 내부에서 application 직접 import 금지.
- **domain은 adapters/application을 모른다.** `domain/model_management`는 순수 타입만.
- **드라이버 의존 금지**: domain/core/ports는 `podman`, `urllib` 등 외부 기술을 모른다.
- 검증: `lint-imports` (4 contracts KEPT) — 편집 전후 필수.

### 1.1 가이드 SSOT 규칙 (rev.2~3 핵심)

**이 가이드의 예제 코드는 최종 상태(final state) 기준이다.** devforge 패키지 import를 전제로 한다:

```python
from devforge.core.database import DatabaseGateway
from devforge.ports.container import InferenceContainerManager
from devforge.domain.model_management.registry import ModelRegistry
```

- 근거: **가이드가 구현의 SSOT**이며, 구현이 가이드를 따른다(중간 상태를 가이드에 박지 않음).
- ⚠️ **Phase 1 초기에는 위 import가 실패할 수 있다(정상).** 모듈이 순차 추가되기 때문이며,
  구현은 해당 모듈을 만든 뒤 그 import를 사용하도록 점진 전환한다. 중간 import 에러는 결함이 아니다.
- 레거시(`scripts/lib/*`)는 **계속 구동**하며, 전환은 devforge 쪽에서 병렬로 진행한다.

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

**Acceptance:** `python3.12 -c "from devforge.ports.container import InferenceContainerManager"`;
`lint-imports` KEPT(ports는 아무것도 import하지 않음).

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
    def get(self, key: str) -> ModelMetadata: ...          # 미존재 → 도메인 예외
    def by_mode(self, mode: str) -> list[ModelMetadata]: ...
```

- **SSOT = devforge (최종 상태, §1.1).** 매핑은 `domain/model_management/metadata.py`에 두고,
  레거시 `scripts/lib/model_registry.py`는 전환기에 `from devforge...` 위임으로 얇게 만든다.
- 전환 전(레거시 위임 적용 전)에는 **패리티 테스트**로 devforge 매핑 vs 레거시 `MODEL_METADATA`
  키/필드 일치를 강제한다.

```python
# tests/unit/test_model_registry.py (패리티)
def test_registry_matches_legacy_metadata():
    from devforge.domain.model_management.metadata import METADATA
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location(
        "legacy_model_registry", "/opt/projects/server/scripts/lib/model_registry.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    assert set(METADATA) == set(mod.MODEL_METADATA)   # 키 일치(필드는 서브셋 검사)
```

**Acceptance:** `get`/`by_mode` 단위 테스트, 패리티 테스트 green.

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
- `dry_run=True`면 podman 미호출(로그만).

**Acceptance:** subprocess mock 단위 테스트 — 정상 / 타임아웃 / 신원 불일치(재시도) / dry_run.

---

## 5. Task 4 — inference CLI 포트 기반 전환

**대상:** `adapters/driving/cli_cmds/inference.py`(현재 socket/subprocess 직접).

- composition root(`devforge/cli.py`)에서 `PodmanInferenceAdapter` + `ModelRegistry` 생성 후
  `inference_cmds.init(manager, registry)`로 주입(전역 setter). (동일 계층 import도 계약 위반은
  아니나 주입 권장.)
- 명령 시그니처 유지: `devforge inference switch <mode>`, `status`, `ensure <key>`.
- `switch`→`switch_mode()`, `ensure`→`ensure_model()`, `status`→`ModelRegistry`+`Paths` 조회.

**Acceptance:** 기존 출력 필드 동일(문자열 회귀), `python3.12 -m devforge.cli inference status` 스모크.
실제 모드 전환은 **운영 창**(§9)에서 1회만.

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

**Acceptance:** `Budget.remaining/expired/gate` 단위 테스트.

---

## 7. Task 6 — 경로 검증 + Phase 1.5 확인

- `core/paths.py` 경로가 실제와 일치하는지 `data/hardcoded_paths.csv`와 대조, 대체 가능한
  잔존 하드코딩 목록화(전면 치환은 Phase 3).
- **Phase 1.5 실측: 이미 대부분 완료.** ADR-0003=Applied, `devforge_shadow`(turns_shadow+
  review_facts_shadow), `scripts/shadow_diff.py`(diff=0 self-verified), `replay_harness.py` 존재.
  잔여는 Phase 3 이후 2주 diff 게이트뿐 — Phase 1에서 새로 만들 것 없음.

---

## 8. 실행 순서 (체크리스트)

1. `cli.py task add "Phase 1 Task 1: ports/container.py"` (추적)
2. Task 1 → 커밋 → Task 2 → 커밋 → Task 3 → 커밋 → Task 4 → 커밋 → Task 5 → 커밋 → Task 6
3. 각 커밋 전 §10 게이트 통과, `lint-imports` KEPT 유지
4. Task 4의 실제 모드 전환만 운영 창에서 수동 1회
5. 완료 시 `REFACTORING_STATUS.yaml` Phase 1 진행 갱신 + handover 로그

---

## 9. 테스트 전략

- **단위(`tests/unit/`)**: `ModelRegistry`(get/by_mode/오류), `PodmanInferenceAdapter`
  (subprocess mock), `Budget`/`PipelineOrchestrator`.
- **패리티**: devforge `ModelRegistry` vs 레거시 `MODEL_METADATA`(키/필드 일치).
- **특성화(`tests/characterization/`)**: Phase 0 자산 재사용. 실제 `switch day→night→day`
  1회는 **운영 창 합의 후 수동**(inference가 파이프라인 처리 중이면 금지).
- 금지: 라이브 LLM/포트 의존 기본 테스트(integration 마커로 분리).

---

## 10. 검증 게이트 (Python 3.12)

```bash
cd /opt/projects/server
python3.12 -m ruff check src/ tests/      # 0 findings
python3.12 -m mypy src/devforge/           # strict, 0 errors
lint-imports                               # 4 contracts KEPT
python3.12 -m pytest tests/unit/ tests/characterization/ -q   # green
scripts/deploy/sync-units.sh --check       # units no drift
```
- [ ] `ports/container.py`가 domain/core/adapters를 import하지 않음
- [ ] `domain/model_management`가 adapters/application을 import하지 않음
- [ ] `devforge inference status` 출력이 현행과 동일
- [ ] 라이브 `scripts/` 파이프라인 무영향

---

## 11. Git 커밋 (원자적 단위)

```bash
git commit -m "feat(phase-1): add InferenceContainerManager port"
git commit -m "feat(phase-1): add ModelRegistry domain model (+ parity test)"
git commit -m "feat(phase-1): add PodmanInferenceAdapter (SubprocessImpl)"
git commit -m "refactor(phase-1): inference CLI uses container port"
git commit -m "feat(phase-1): PipelineOrchestrator + BudgetManager sketch"
```
- 커밋별 §10 게이트 통과 유지. §1.1 SSOT 규칙과 중간 import 예외를 본문에 기록.

---

## 12. 리스크 및 완화

| 리스크 | 완화 |
|--------|------|
| 중간 단계 devforge import 실패 | §1.1 문서화(정상), 모듈 추가 후 점진 전환 |
| 레거시 MODEL_METADATA 드리프트 | 패리티 테스트로 불일치 차단 |
| 유닛 미러 vs live 드리프트 | `sync-units.sh --check` (+ 선택: daily 타이머 알림) |
| auto-sync가 수동 커밋 흡수 | `docs '*.md'` 제외 적용. (선택) `Dockerfile`·`.github/`도 제외 확대 |
| 컨테이너 재시작 지연(~70s) | dry_run/mock 테스트 + 실제 1회 수동 검증 |
| 과설계 | 오케스트레이터는 골격만, 단계는 Phase 3 |

---

## 13. 다음 단계 / 이월

- **Phase 2:** Watchdog 도메인화 + IssueCollector.
- **Phase 3:** 파이프라인 단계 모듈화 + `day_cycle.sh` → `PipelineOrchestrator`.
- **컷오버 이월(Step 4/5):** 호스트 user unit을 3.12로 전환 = legacy scripts를 3.12에서 실행 →
  legacy 호환성 검증 필요 → **컷오버(Phase A~I)** 에서 처리. 현재 대상 유닛 0개.
- **별도 보안:** 세션 검증 강화 — 쿠키 서명(itsdangerous) 또는 서버측 저장소.
  현황: `or True` 제거 완료(e1512f7)했으나 쿠키 서명/서버측 세션은 미구현(위조 가능성 잔존).
