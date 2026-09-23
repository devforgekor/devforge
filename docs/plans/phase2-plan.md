# Phase 2 구현 가이드 — Watchdog 도메인화

**Status:** done — Phase 2 v2.1 COMPLETE(18 tasks); shadow-run(2.5) 진행 · **Date:** 2026-09-22 (rev.1) · **Owner:** devforge
**대상:** AI 에이전트 또는 개발자 · **난이도:** High · **예상:** 3–4일 (Week 5–6)
**선행:** Phase 0+1 complete. **정본:** `REFACTORING_PLAN.md` / `REFACTORING_STATUS.yaml`

---

## 0. 컨텍스트

### 0.1 레거시 구조 (단일 모놀리식)

```
scripts/watchdog.py (13줄, 진입점)
└── lib/watchdog/orchestrator.py (1122줄 — 모든 로직)
    ├── main_loop / run_day_checks / day_fix_loop
    ├── _run_services / _run_timers / _run_memory_check
    ├── _run_svcpod_forwarding / _consume_actions
    └── build_heartbeat_summary
lib/watchdog/checker.py (784줄 — 건강 체크)
lib/watchdog/state.py (580줄 — 상태 관리)
lib/watchdog/fixloop.py (291줄) / notifier.py (376줄)
lib/watchdog/recovery.py (463줄) / incidents.py (288줄)
```

**문제:** orchestrator.py 1122줄 비대, I/O와 순수 로직 혼재, 테스트 어려움.

### 0.2 목표 구조

```
domain/watchdog/         ← 순수 비즈니스 로직 (I/O 의존 0)
├── model.py             ← HealthCheckResult, HeartbeatResult, Alert
├── sentinel.py          ← DB Sentinel 비즈니스 로직
├── heartbeat.py         ← heartbeat 판정 로직
├── health.py            ← 건강 체크 판정 로직
└── alert.py             ← 알림 규칙 판정 로직
adapters/driven/watchdog/ ← I/O 캡슐화
├── db_repository.py     ← DB 접근 (실제 쿼리)
├── slack_notifier.py    ← Slack API (실제 호출)
└── file_health.py       ← 파일 시스템 체크
application/watchdog/     ← orchestrator (골격)
└── orchestrator.py       ← watchdog 흐름 관리
adapters/driving/watchdog/ ← 실행 루프
└── watcher.py            ← systemd에서 호출
```

### 0.3 레거시 매핑 (핵심 모듈)

| devforge | 레거시 | 역할 |
|----------|--------|------|
| `domain/watchdog/model.py` | `state.py:ComponentState,ComponentTracker` | 타입 정의 |
| `domain/watchdog/sentinel.py` | `state.py:WatchdogState` (I/O 분리) | DB Sentinel 로직 |
| `domain/watchdog/heartbeat.py` | `checker.py:check_heartbeats,check_all_llm` | heartbeat 판정 |
| `domain/watchdog/health.py` | `checker.py:check_health,check_service,check_memory` | 건강 체크 판정 |
| `domain/watchdog/alert.py` | `notifier.py:SlackNotifier` (규칙만) | 알림 규칙 |
| `adapters/.../db_repository.py` | `state.py:WatchdogState` (DB I/O) | DB 접근 |
| `adapters/.../slack_notifier.py` | `notifier.py:SlackNotifier` (API) | Slack 알림 |
| `adapters/.../file_health.py` | `checker.py:check_health,check_disk` | 파일 체크 |
| `application/watchdog/orchestrator.py` | `orchestrator.py:run_day_checks,day_fix_loop` | 흐름 관리 |
| `adapters/driving/watchdog/watcher.py` | `orchestrator.py:main_loop` | 실행 루프 |

---

## 1. 아키텍처 제약

```
application > pipeline_stages > adapters > domain > core > ports
```

- 역방향 import 금지, composition root 규칙, domain은 I/O 모름.
- 검증: `lint-imports` (4 contracts KEPT).

### 1.1 SSOT 규칙 (Phase 1과 동일)

예제 코드는 **최종 상태(final state)** 기준. 중간 import 에러는 정상.

---

## 2. Tasks 상세

### Task 1: `domain/watchdog/model.py`
```python
class ComponentStatus(Enum):
    HEALTHY = "healthy"; DEGRADED = "degraded"; UNHEALTHY = "unhealthy"; UNKNOWN = "unknown"

@dataclass(frozen=True)
class HealthCheckResult:
    component: str; status: ComponentStatus; message: str = ""
    checked_at: datetime = field(default_factory=datetime.utcnow)

@dataclass(frozen=True)
class HeartbeatResult:
    agent_id: str; alive: bool; last_seen: datetime | None = None; detail: str = ""

@dataclass(frozen=True)
class Alert:
    severity: str; component: str; message: str
    created_at: datetime = field(default_factory=datetime.utcnow)
```

### Task 2: `domain/watchdog/sentinel.py`
```python
class SentinelRepository(Protocol):
    def get_mode(self) -> str: ...
    def get_component_state(self, name: str) -> dict | None: ...
    def update_component_state(self, name: str, state: dict) -> None: ...

class Sentinel:
    def __init__(self, repo: SentinelRepository) -> None: ...
    def should_check(self, name: str, interval_sec: int = 60) -> bool: ...
    def record_check(self, name: str, status: ComponentStatus, detail: str = "") -> None: ...
```

### Task 3: `domain/watchdog/heartbeat.py`
```python
class HeartbeatChecker:
    def __init__(self, stale_threshold_sec: int = 300) -> None: ...
    def is_stale(self, result: HeartbeatResult) -> bool: ...
    def filter_stale(self, results: list[HeartbeatResult]) -> list[HeartbeatResult]: ...
```

### Task 4: `domain/watchdog/health.py`
```python
class HealthChecker:
    def __init__(self, memory_threshold_gb: float = 2.0) -> None: ...
    def evaluate_health(self, result: HealthCheckResult) -> ComponentStatus: ...
    def check_memory_budget(self, used_gb: float, total_gb: float) -> HealthCheckResult: ...
```

### Task 5: `domain/watchdog/alert.py`
```python
class AlertRule:
    def __init__(self, component: str, threshold: str = "unhealthy") -> None: ...
    def evaluate(self, result: HealthCheckResult) -> Alert | None: ...
```

### Task 6: `adapters/driven/watchdog/db_repository.py`
```python
class WatchdogDBRepository(SentinelRepository):
    def get_mode(self) -> str: ...  # DB 쿼리
    def get_component_state(self, name: str) -> dict | None: ...
    def update_component_state(self, name: str, state: dict) -> None: ...
```

### Task 7: `adapters/driven/watchdog/slack_notifier.py`
```python
class SlackNotifier:
    def __init__(self, webhook_url: str) -> None: ...
    def send(self, alert: Alert) -> bool: ...  # urllib.request로 Slack API 호출
```

### Task 8: `adapters/driven/watchdog/file_health.py`
```python
class FileHealthAdapter:
    def check_disk_usage(self, path: str) -> HealthCheckResult: ...
```

### Task 9: `application/watchdog/orchestrator.py` (골격)
```python
class WatchdogOrchestrator:
    def __init__(self, sentinel, heartbeat_checker, health_checker, alert_rules): ...
    def run_checks(self, dry_run: bool = False) -> dict: ...
```

### Task 10: `adapters/driving/watchdog/watcher.py`
```python
def main_loop(one_shot: bool = False, dry_run: bool = False) -> None: ...
```

---

## 3. 검증 게이트

```bash
python3.12 -m ruff check src/devforge/domain/watchdog/ src/devforge/adapters/*/watchdog/ src/devforge/application/watchdog/
python3.12 -m mypy src/devforge/domain/watchdog/ src/devforge/adapters/*/watchdog/ src/devforge/application/watchdog/
lint-imports  # 4 KEPT
python3.12 -m pytest tests/unit/ tests/characterization/ -q  # green
```
- [ ] `domain/watchdog/`가 adapters/application import하지 않음
- [ ] 기존 `scripts/lib/watchdog/` 동작 무영향
- [ ] 기존 테스트 통과 유지

---

## 4. 커밋 전략

```bash
git commit -m "feat(phase-2): add watchdog domain model"
git commit -m "feat(phase-2): add Sentinel domain logic (pure)"
git commit -m "feat(phase-2): add HeartbeatChecker domain logic (pure)"
git commit -m "feat(phase-2): add HealthChecker domain logic (pure)"
git commit -m "feat(phase-2): add AlertRule domain logic (pure)"
git commit -m "feat(phase-2): add WatchdogDBRepository adapter"
git commit -m "feat(phase-2): add SlackNotifier adapter"
git commit -m "feat(phase-2): add FileHealthAdapter"
git commit -m "feat(phase-2): add WatchdogOrchestrator (skeleton)"
git commit -m "feat(phase-2): add watcher execution loop"
```

---

## 5. 리스크

| 리스크 | 완화 |
|--------|------|
| orchestrator.py 1122줄 분할 | 순차 분할, 각 모듈 독립 테스트 |
| 레거시 동작 재현 실패 | 특성화 테스트 재사용 |
| DB 스키마 의존 | `SentinelRepository` 포트로 격리 |
| 과설계 | 골격만, 단계별 구현 |

---

## 6. 다음 단계

- **Phase 3:** 파이프라인 단계 모듈화 + `day_cycle.sh` → `PipelineOrchestrator`.
- **컷오버:** Phase 2 완료 후 기존 `scripts/lib/watchdog/` → `devforge` 전환 검토.
