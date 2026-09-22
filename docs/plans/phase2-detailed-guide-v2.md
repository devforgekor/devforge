# Phase 2 Detailed Implementation Guide — Watchdog Subsystem
## v2.1 — Legacy-Complete, Phase A–E

> **STATUS**: COMPLETE — 18 tasks (A1–A5, B1–B3, C1–C4, D1–D3, E1–E3)
> **Design Philosophy**: 100% behavior parity with `scripts/lib/watchdog` (production SSOT)
> **Predecessor**: v2.0 DRAFT (1,150 lines, Phase A only). This version fixes all Phase A defects found in review and adds Phases B–E.
> **Scope**: `scripts/lib/watchdog` (4,706 lines, 11 modules) → `src/devforge/domain/watchdog` + adapters

**Reading order**: Section 0 (Legacy Ground Truth) is authoritative. When any code below disagrees with it, Section 0 wins.

---

## 0. Legacy Ground Truth (must-read conventions)

All conventions are extracted from production code. Cite these line numbers, not the idealized examples.

### 0.1 State machine (`state.py:21-25, 113-132`)

| State | Trigger | Circuit |
|-------|---------|---------|
| `HEALTHY` | initial / any success | CLOSED |
| `DEGRADED` | `consecutive_fail` 1–2 | CLOSED |
| `UNHEALTHY` | `consecutive_fail >= 3` | **OPEN** for `CIRCUIT_BREAKER_TIMEOUT` |
| `DOWN` | `consecutive_fail >= 5` | OPEN (escalated) |

- Success always → `HEALTHY`, `consecutive_fail = 0`, `circuit_open_until = 0`.
- `fail_count` (total) resets **only** after `BACKOFF_RESET_SEC` of health (`state.py:106-108`).

### 0.2 Constants (`config.py:126-129, state.py:38`)

```python
BACKOFF_SCHEDULE = [0, 10, 20, 40, 80, 120, 300]   # seconds
BACKOFF_RESET_SEC = 600                            # 10 min healthy → reset fail_count
CIRCUIT_BREAKER_TIMEOUT = 120                      # 2 min OPEN → HALF_OPEN
TREND_MAX_SAMPLES = 60                             # state.py:38
ALERT_DEDUP_SEC = 300                              # 5 min per-component alert dedup
```

### 0.3 Tracker key namespace (`orchestrator.py`)

Trackers and incidents are keyed by a **namespaced string**, not a bare name:

| Namespace | Example | Check source |
|-----------|---------|--------------|
| `svc:` | `svc:devforge-turn-watcher` | systemd user services (`check_all_services`) |
| `timer:` | `timer:devforge-day-cycle.timer` | systemd timers (`check_all_timers`) |
| `syssvc:` | `syssvc:caddy` | system services, alert-only (`check_all_system_services`) |
| `oneshot:` | `oneshot:news-collector` | oneshot results (`check_all_oneshot_results`) |
| `llm:` | `llm:day-extract` | LLM probes (`check_all_llm`) |
| `pipeline:` | `pipeline:day_cycle` | pipeline process (`check_pipeline`) |
| `system:` | `system:memory` | memory/disk (`check_memory`) |
| `infra:` | `infra:inference` | inference container cascade |
| `fix:` | `fix:day_cycle` | fix-loop pulse (`orchestrator.py:559`) |

### 0.4 Notification signatures (`notifier.py:345, 362`)

```python
def send_alert(component: str, state: str, detail: str) -> None: ...
def send_recovery(component: str, detail: str) -> None: ...
def sd_notify(state: str) -> bool: ...
```

- Slack uses the **Web API** (`chat.postMessage` / `chat.update`) with `SLACK_BOT_TOKEN_KEY` + `SLACK_CHANNEL` — **not** a webhook URL.
- `state` is the state label string, e.g. `tracker.state.value` or `"DOWN"` / `"LATENCY"` / `"DELAY"`.

### 0.5 Incident signatures (`incidents.py:198, 230, 247`)

```python
def record_detect(component: str, event_type: str, detail: str,
                  unit: str | None = None) -> int | None: ...
def record_action(incident_id: int | None, action: str, ok: bool) -> None: ...
def resolve_if_open(component: str) -> None: ...
```

- `dedup_key = f"{component}:{event_type}"`.
- Lifecycle: open → resolved; reopen within `REOPEN_WINDOW_SEC=3600`.
- Table `watchdog_incidents` columns are the SSOT (see Task E1).

### 0.6 Heartbeats (`messenger.py:155-186`)

- A heartbeat is a `watchdog_pulses` row: `pulse_id = 'heartbeat_<worker>'`, `status='IN_PROGRESS'`, `created_at` is the beat.
- Stale if `now - created_at >= max_age` (`HEARTBEAT_WORKERS[worker]`, default 1800s).
- `RESOLVED`/`IGNORED` heartbeats count as alive.

### 0.7 Metrics / ETA (`state.py:48-71`)

- `TrendTracker.predict_eta(threshold)` = linear regression over a ring buffer.
- **Slope check comes before breach check**; returns minutes.
- Requires ≥10 samples; descending/flat → `None`.

---

## Phase 0: Existing Code Reconciliation (REQUIRED before Phase A)

The current `src/devforge` tree already contains a **Phase 2 v1 implementation** (commits `2c8e936`, `064afa1`) built against the redesign (states `CRITICAL/RECOVERING`, types `HealthCheckResult/ComponentStatus`, `record_check`). v2.1 replaces it. Do this first, in one commit, so later tasks land on a clean base.

### 0.1 Files to delete (superseded)

```
src/devforge/domain/watchdog/monitoring/tracker.py        (replaced by A3/A4)
src/devforge/domain/watchdog/monitoring/circuit_breaker.py (folded into A3)
src/devforge/domain/watchdog/model.py                     (→ ports/types.py, A1)
src/devforge/domain/watchdog/recovery/strategies.py       (replaced by B1)
src/devforge/domain/watchdog/recovery/graduation.py       (replaced by B2)
src/devforge/domain/watchdog/orchestration/check_coordinator.py (replaced by B3)
src/devforge/domain/watchdog/orchestration/fix_coordinator.py   (folded into B2)
src/devforge/ports/health_check.py                        (rewritten: batch HealthCheck)
src/devforge/ports/incident_repository.py                 (rewritten: legacy ops)
src/devforge/ports/notification.py                        (rewritten: legacy signatures)
src/devforge/ports/recovery.py                            (rewritten: RecoveryAction.kind)
```

### 0.2 Tests to delete / replace

```
tests/unit/test_component_tracker.py
tests/unit/test_circuit_breaker.py
tests/unit/test_graduated_recovery.py
tests/unit/test_recovery_coordinator.py
tests/unit/test_watchdog_model.py
tests/unit/test_watchdog_coordinators.py
tests/unit/test_watchdog_service.py
tests/unit/test_incident_pg.py
tests/unit/test_watchdog_config.py
tests/characterization/test_watchdog_state_parity.py
tests/characterization/test_watchdog_{systemd,llm,recovery}_parity.py
```

`tests/characterization/test_watchdog.py` (liveness) stays — it pins legacy behavior we preserve.

### 0.3 Files kept as-is

`core/` (config/paths/exceptions/logging/database), `domain/models.py` (**has `WatchdogIncident` ORM model — reuse it in E1**), `adapters/driven/storage/database_gateway.py`.

### 0.4 Verification

```bash
cd /opt/projects/server
git rm -r src/devforge/domain/watchdog/monitoring/tracker.py ...   # per 0.1
git rm tests/unit/test_component_tracker.py ...                    # per 0.2
lint-imports && pytest tests/unit tests/characterization -q
```

Commit: `refactor(watchdog): drop v1 Phase 2 implementation for legacy-parity v2`

---

## Phase 1 Completion Verification (45 min)

Run **before** Phase 2. Details at the end of this document (Verification Gates → Gate 0).

Checklist:
- [ ] `core/{database,paths,exceptions,config}` exist
- [ ] `ports/container.py`, `domain/model_management/registry.py`, `adapters/driven/container/podman_adapter.py`, `application/orchestrator.py` exist
- [ ] `lint-imports` → 4 contracts KEPT
- [ ] `pytest tests/unit tests/characterization -q` → green
- [ ] `devforge inference status` runs without error

---

## Architecture Vision

### Target (Hexagonal)

```
src/devforge/
├── ports/
│   ├── types.py                 ← ComponentState, HealthCheck, RecoveryAction, CircuitState, Incident
│   ├── health_check.py          ← HealthCheckPort (batch: check_health() -> list[HealthCheck])
│   ├── recovery.py              ← RecoveryPort (execute_recovery(action) -> bool)
│   ├── notification.py          ← NotificationPort (send_alert/send_recovery/sd_notify)
│   ├── incident_repository.py   ← record_detect/record_action/resolve_if_open/find_open
│   └── state_persistence.py     ← StateStoragePort (save/load legacy JSON)
├── domain/watchdog/
│   ├── monitoring/
│   │   ├── backoff.py           ← BackoffCalculator (A2)
│   │   └── tracker.py           ← ComponentTracker, TrackerRegistry, TrendTracker (A3/A4)
│   ├── recovery/
│   │   ├── strategies.py        ← recovery kind classification (B1)
│   │   └── graduation.py        ← RecoveryCoordinator (graduated_recover, B2)
│   └── orchestration/
│       └── check_coordinator.py ← CheckCoordinator (B3)
├── adapters/driven/
│   ├── health/{systemd,llm,system,pipeline}_health.py   (C1–C4)
│   ├── notification/{slack,systemd}_notifier.py         (D1–D2)
│   ├── recovery/systemd_recovery.py                     (D3)
│   └── storage/{incident_pg,state_json,messenger_pg}.py (E1 / A5 / E-optional)
├── adapters/driving/cli_cmds/watchdog.py                (E3)
└── application/watchdog_service.py                      (E2)
```

**Why types live in `ports/types.py`**: import-linter `hexagonal`/`layering` place `ports` as the lowest layer with no internal deps; `domain` may import `ports` but not vice-versa. Putting shared value objects in `ports/` lets both sides import them without inversion. Precedent: `ports/container.py`.

---

## Legacy Feature Matrix

| Feature | Legacy | Target | Priority |
|---------|--------|--------|----------|
| State enum | `state.py:21` | `ports/types.py` `ComponentState` | P0 |
| CrashLoopBackOff | `state.py:160`, `config.py:126` | `monitoring/backoff.py` | P0 |
| State persistence | `state.py:290-336`, `config.py:34` | `ports/state_persistence.py` + `storage/state_json.py` | P0 |
| Circuit HALF_OPEN | `state.py:134` | `monitoring/tracker.py` `can_retry` | P0 |
| Alert dedup | `state.py:148` | `monitoring/tracker.py` `can_alert` | P1 |
| TrendTracker ETA | `state.py:48-71` | `monitoring/tracker.py` `TrendTracker` | P1 |
| Tracker registry/keys | `state.py:229`, `orchestrator.py` | `monitoring/tracker.py` `TrackerRegistry` | P0 |
| graduated_recover | `recovery.py:270-297` | `recovery/graduation.py` | P0 |
| Health checks | `checker.py` | `adapters/driven/health/*` | P0 |
| Notifications | `notifier.py` | `adapters/driven/notification/*` | P1 |
| Incidents | `incidents.py` | `adapters/driven/storage/incident_pg.py` | P1 |
| Heartbeats | `messenger.py:155` | `adapters/driven/health/pipeline_health.py` | P1 |
| codescanner / fixloop | `codescanner.py`, `fixloop.py` | **out of scope (Phase 3)** | P3/P4 |

---

# Phase A: Core Domain Logic (Day 1–2, 5 Tasks, 37 tests)

## Task A1: Value Objects (`ports/types.py`)

```python
# src/devforge/ports/types.py
"""Shared value objects for the watchdog subsystem.

Lives in ports/ (not domain/) so domain, adapters, and application can all
import it without layer inversion (import-linter: ports is the lowest layer).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ComponentState(Enum):
    """Legacy state machine (state.py:21-25)."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"    # 1-2 consecutive failures, still retrying
    UNHEALTHY = "UNHEALTHY"  # 3+ consecutive failures, circuit OPEN
    DOWN = "DOWN"            # 5+ consecutive failures, escalated


@dataclass(frozen=True)
class HealthCheck:
    """Result of one component check (per-component, not aggregated)."""
    component: str          # namespaced tracker key, e.g. "svc:devforge-fastapi"
    is_healthy: bool
    detail: str
    metric_value: Optional[float] = None   # e.g. memory %, latency ms
    threshold: Optional[float] = None


@dataclass(frozen=True)
class RecoveryAction:
    """A recovery to be executed by a driven adapter.

    `kind` selects the adapter command and mirrors the legacy recover_* family
    (recovery.py) — there is no soft/medium/hard enum in production.
    """
    component: str          # namespaced tracker key
    kind: str               # "service" | "container" | "svcpod" | "oneshot" | "cascade" | "pipeline"
    reason: str
    backoff_sec: int = 0


@dataclass
class CircuitState:
    """Circuit breaker snapshot (state.py:134-141)."""
    is_open: bool
    failure_count: int
    opens_at: Optional[float] = None   # monotonic timestamp
    can_retry: bool = True


@dataclass(frozen=True)
class Incident:
    """Mirror of the production watchdog_incidents table (incidents.py:41-60)."""
    id: Optional[int]
    dedup_key: str
    component: str
    status: str
    symptom: Optional[str]
    context: Optional[str]
    detected_at: datetime
    last_seen_at: datetime
    action: Optional[str] = None
    action_result: Optional[str] = None
    action_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    fail_count: int = 1
    reopen_count: int = 0
```

**Tests**: `tests/unit/ports/test_types.py` (5)

```python
import pytest
from dataclasses import FrozenInstanceError
from devforge.ports.types import ComponentState, HealthCheck, RecoveryAction, Incident
from datetime import datetime, timezone


def test_component_state_matches_legacy():
    assert [s.value for s in ComponentState] == ["HEALTHY", "DEGRADED", "UNHEALTHY", "DOWN"]


def test_health_check_is_frozen():
    hc = HealthCheck(component="svc:x", is_healthy=True, detail="OK")
    with pytest.raises(FrozenInstanceError):
        hc.is_healthy = False  # type: ignore[misc]


def test_health_check_optional_metrics_default_none():
    hc = HealthCheck(component="svc:x", is_healthy=False, detail="down")
    assert hc.metric_value is None and hc.threshold is None


def test_recovery_action_default_backoff():
    a = RecoveryAction(component="svc:x", kind="service", reason="down")
    assert a.backoff_sec == 0


def test_incident_defaults():
    now = datetime.now(timezone.utc)
    i = Incident(id=None, dedup_key="svc:x:down", component="svc:x", status="open",
                 symptom="down", context=None, detected_at=now, last_seen_at=now)
    assert i.fail_count == 1 and i.reopen_count == 0 and i.resolved_at is None
```

Commit: `feat(phase2): add watchdog value objects (ports/types.py)`

---

## Task A2: CrashLoopBackOff with Jitter (`monitoring/backoff.py`)

Legacy: `state.py:160-171`, `config.py:126`.

```python
# src/devforge/domain/watchdog/monitoring/backoff.py
"""CrashLoopBackOff with jitter (legacy state.py:160-171)."""
from __future__ import annotations

import random
from typing import List, Optional

DEFAULT_BACKOFF_SCHEDULE: List[int] = [0, 10, 20, 40, 80, 120, 300]


class BackoffCalculator:
    """Schedule-indexed backoff with ±10% jitter (anti retry-storm)."""

    def __init__(self, schedule: Optional[List[int]] = None) -> None:
        self.schedule = schedule or DEFAULT_BACKOFF_SCHEDULE

    def calculate(self, attempt: int) -> int:
        """Backoff seconds for `attempt` consecutive failures (legacy order)."""
        idx = min(attempt, len(self.schedule) - 1)
        base = self.schedule[idx]
        if base == 0:
            return 0
        return int(base * random.uniform(0.9, 1.1))

    def max_backoff(self) -> int:
        return self.schedule[-1]
```

**Tests**: `tests/unit/domain/watchdog/monitoring/test_backoff.py` (6)

```python
from devforge.domain.watchdog.monitoring.backoff import BackoffCalculator, DEFAULT_BACKOFF_SCHEDULE


def test_schedule_matches_legacy():
    assert DEFAULT_BACKOFF_SCHEDULE == [0, 10, 20, 40, 80, 120, 300]


def test_zero_attempt_returns_zero():
    assert BackoffCalculator().calculate(0) == 0


def test_backoff_jitter_ranges():
    calc = BackoffCalculator()
    assert 9 <= calc.calculate(1) <= 11      # 10 ±10%
    assert 18 <= calc.calculate(2) <= 22     # 20 ±10%
    assert 36 <= calc.calculate(3) <= 44     # 40 ±10%


def test_backoff_caps_at_max():
    calc = BackoffCalculator()
    assert 270 <= calc.calculate(100) <= 330  # 300 ±10%


def test_jitter_varies():
    calc = BackoffCalculator()
    assert len({calc.calculate(3) for _ in range(20)}) > 1


def test_custom_schedule_is_range_asserted():
    # NOTE: base=5 with ±10% jitter yields 4..5, so assert a range (v2.0 bug: == 5)
    calc = BackoffCalculator(schedule=[5, 10, 15])
    assert 4 <= calc.calculate(0) <= 5
    assert calc.max_backoff() == 15
```

> v2.0 defect fixed: the doctest `# 42` was removed (jitter is nondeterministic) and `test_custom_schedule` now asserts a range.

Commit: `feat(phase2): add CrashLoopBackOff calculator with jitter`

---

## Task A3: ComponentTracker + Registry (`monitoring/tracker.py`)

Legacy: `state.py:79-211, 229-232`.

```python
# src/devforge/domain/watchdog/monitoring/tracker.py
"""Component state tracker, registry, and circuit breaker (legacy state.py:79-211)."""
from __future__ import annotations

import time
from typing import Dict, Optional

from devforge.domain.watchdog.monitoring.backoff import BackoffCalculator
from devforge.ports.types import CircuitState, ComponentState, HealthCheck

CIRCUIT_BREAKER_TIMEOUT = 120   # config.py:129
BACKOFF_RESET_SEC = 600         # config.py:128
DEFAULT_ALERT_DEDUP_SEC = 300   # state.py:148


class ComponentTracker:
    """Tracks state + failure counts for ONE component key."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.state = ComponentState.HEALTHY
        self.fail_count = 0
        self.consecutive_fail = 0
        self.last_state_change = 0.0
        self.last_alert_ts = 0.0
        self.last_success_ts = time.monotonic()
        self.last_fail_ts = 0.0
        self.circuit_open_until = 0.0
        self._backoff = BackoffCalculator()

    # ── record ──────────────────────────────────────────────────────
    def record_success(self) -> None:
        """Reset consecutive counter; full reset after BACKOFF_RESET_SEC healthy."""
        now = time.monotonic()
        self.consecutive_fail = 0
        self.last_success_ts = now
        if self.fail_count > 0 and (now - self.last_fail_ts) >= BACKOFF_RESET_SEC:
            self.fail_count = 0
        self._transition(ComponentState.HEALTHY)
        self.circuit_open_until = 0.0

    def record_failure(self) -> bool:
        """Record a failure; return True if the state changed."""
        now = time.monotonic()
        self.consecutive_fail += 1
        self.fail_count += 1
        self.last_fail_ts = now

        old = self.state
        if self.consecutive_fail >= 5:
            self._transition(ComponentState.DOWN)
        elif self.consecutive_fail >= 3:
            self._transition(ComponentState.UNHEALTHY)
            self.circuit_open_until = now + CIRCUIT_BREAKER_TIMEOUT
        else:
            self._transition(ComponentState.DEGRADED)
        return old != self.state

    def record_check(self, check: HealthCheck) -> bool:
        """Bridge from a HealthCheck to record_success/record_failure.

        Kept so driven health adapters (which produce HealthCheck) can drive the
        tracker without knowing the two-method legacy API.
        """
        if check.is_healthy:
            self.record_success()
            return False
        return self.record_failure()

    # ── circuit / alert ─────────────────────────────────────────────
    def can_retry(self) -> bool:
        """CLOSED or HALF_OPEN (timeout elapsed) → True (state.py:134-141)."""
        if self.circuit_open_until == 0.0:
            return True
        if time.monotonic() >= self.circuit_open_until:
            self.circuit_open_until = 0.0
            return True
        return False

    def can_alert(self, dedup_sec: int = DEFAULT_ALERT_DEDUP_SEC) -> bool:
        now = time.monotonic()
        if now - self.last_alert_ts >= dedup_sec:
            self.last_alert_ts = now
            return True
        return False

    def is_degraded(self) -> bool:
        return self.state in (ComponentState.DEGRADED, ComponentState.UNHEALTHY, ComponentState.DOWN)

    def backoff_sec(self) -> int:
        return self._backoff.calculate(self.consecutive_fail)

    def circuit_status(self) -> CircuitState:
        is_open = self.circuit_open_until > time.monotonic()
        return CircuitState(is_open=is_open, failure_count=self.consecutive_fail,
                            opens_at=self.circuit_open_until if is_open else None,
                            can_retry=self.can_retry())

    # ── persistence ─────────────────────────────────────────────────
    def summary(self) -> Dict[str, object]:
        return {"name": self.name, "state": self.state.value, "fail_count": self.fail_count,
                "consecutive_fail": self.consecutive_fail,
                "circuit_open": self.circuit_open_until > time.monotonic()}

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.name, "state": self.state.value, "fail_count": self.fail_count,
                "consecutive_fail": self.consecutive_fail,
                "last_state_change": self.last_state_change, "last_alert_ts": self.last_alert_ts,
                "last_success_ts": self.last_success_ts, "last_fail_ts": self.last_fail_ts,
                "circuit_open_until": self.circuit_open_until}

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "ComponentTracker":
        t = cls(str(data.get("name", "unknown")))
        try:
            t.state = ComponentState(data.get("state", "HEALTHY"))
        except ValueError:
            t.state = ComponentState.HEALTHY
        t.fail_count = int(data.get("fail_count", 0))            # type: ignore[arg-type]
        t.consecutive_fail = int(data.get("consecutive_fail", 0))  # type: ignore[arg-type]
        t.last_state_change = float(data.get("last_state_change", 0.0))  # type: ignore[arg-type]
        t.last_alert_ts = float(data.get("last_alert_ts", 0.0))    # type: ignore[arg-type]
        t.last_success_ts = float(data.get("last_success_ts", time.monotonic()))  # type: ignore[arg-type]
        t.last_fail_ts = float(data.get("last_fail_ts", 0.0))      # type: ignore[arg-type]
        t.circuit_open_until = float(data.get("circuit_open_until", 0.0))  # type: ignore[arg-type]
        return t

    def _transition(self, new_state: ComponentState) -> None:
        if self.state != new_state:
            self.state = new_state
            self.last_state_change = time.monotonic()


class TrackerRegistry:
    """Aggregate of per-key trackers (legacy WatchdogState._components, state.py:229)."""

    def __init__(self) -> None:
        self._trackers: Dict[str, ComponentTracker] = {}

    def get(self, key: str) -> ComponentTracker:
        if key not in self._trackers:
            self._trackers[key] = ComponentTracker(key)
        return self._trackers[key]

    def all(self) -> Dict[str, ComponentTracker]:
        return dict(self._trackers)

    def restore(self, data: Dict[str, object]) -> None:
        for key, raw in (data or {}).items():
            try:
                self._trackers[key] = ComponentTracker.from_dict(raw)  # type: ignore[arg-type]
            except Exception:
                continue
```

**Tests**: `tests/unit/domain/watchdog/monitoring/test_tracker.py` (14)

```python
import time
from devforge.domain.watchdog.monitoring.tracker import (
    ComponentTracker, TrackerRegistry, CIRCUIT_BREAKER_TIMEOUT)
from devforge.ports.types import ComponentState, HealthCheck


def test_initial_state_healthy():
    t = ComponentTracker("svc:x")
    assert t.state == ComponentState.HEALTHY and t.consecutive_fail == 0


def test_one_failure_degraded():
    t = ComponentTracker("svc:x")
    assert t.record_failure() is True
    assert t.state == ComponentState.DEGRADED and t.consecutive_fail == 1


def test_three_failures_unhealthy():
    t = ComponentTracker("svc:x")
    for _ in range(3):
        t.record_failure()
    assert t.state == ComponentState.UNHEALTHY and t.consecutive_fail == 3


def test_five_failures_down():
    t = ComponentTracker("svc:x")
    for _ in range(5):
        t.record_failure()
    assert t.state == ComponentState.DOWN


def test_circuit_opens_at_three():
    t = ComponentTracker("svc:x")
    for _ in range(3):
        t.record_failure()
    assert t.circuit_open_until > 0 and t.can_retry() is False


def test_circuit_half_open_after_timeout():
    t = ComponentTracker("svc:x")
    for _ in range(3):
        t.record_failure()
    t.circuit_open_until = time.monotonic() - 1
    assert t.can_retry() is True


def test_success_resets_consecutive_not_total():
    t = ComponentTracker("svc:x")
    t.record_failure(); t.record_failure()
    t.record_success()
    assert t.consecutive_fail == 0 and t.fail_count == 2
    assert t.state == ComponentState.HEALTHY


def test_alert_dedup():
    t = ComponentTracker("svc:x")
    assert t.can_alert(dedup_sec=10) is True
    assert t.can_alert(dedup_sec=10) is False
    t.last_alert_ts = time.monotonic() - 11
    assert t.can_alert(dedup_sec=10) is True


def test_backoff_grows():
    t = ComponentTracker("svc:x")
    assert t.backoff_sec() == 0
    t.record_failure()
    assert 9 <= t.backoff_sec() <= 11


def test_backoff_reset_after_10min():
    t = ComponentTracker("svc:x")
    t.record_failure(); t.record_failure()
    t.last_fail_ts = time.monotonic() - 601
    t.record_success()
    assert t.fail_count == 0


def test_record_check_bridge_success():
    t = ComponentTracker("svc:x")
    assert t.record_check(HealthCheck(component="svc:x", is_healthy=True, detail="ok")) is False
    assert t.state == ComponentState.HEALTHY


def test_record_check_bridge_failure():
    t = ComponentTracker("svc:x")
    assert t.record_check(HealthCheck(component="svc:x", is_healthy=False, detail="down")) is True
    assert t.state == ComponentState.DEGRADED


def test_serialization_roundtrip():
    t = ComponentTracker("svc:x")
    t.record_failure(); t.record_failure(); t.can_alert()
    r = ComponentTracker.from_dict(t.to_dict())
    assert r.name == "svc:x" and r.state == ComponentState.DEGRADED
    assert r.consecutive_fail == 2 and r.last_alert_ts > 0


def test_registry_get_creates_and_restores():
    reg = TrackerRegistry()
    assert reg.get("svc:a").name == "svc:a"
    assert set(reg.all()) == {"svc:a"}
    t = ComponentTracker("svc:b"); t.record_failure()
    reg2 = TrackerRegistry(); reg2.restore({"svc:b": t.to_dict()})
    assert reg2.get("svc:b").consecutive_fail == 1
```

Commit: `feat(phase2): add ComponentTracker, TrackerRegistry, circuit breaker`

---

## Task A4: TrendTracker with ETA (`monitoring/tracker.py`, append)

Legacy: `state.py:28-77`. **Parity-critical**: slope check precedes breach check; window = 60; time in seconds; return minutes.

```python
# Append to src/devforge/domain/watchdog/monitoring/tracker.py
import time as _time
from typing import List as _List, Tuple as _Tuple

TREND_MAX_SAMPLES = 60  # state.py:38


class TrendTracker:
    """Metric trend + linear-regression ETA (legacy state.py:28-77)."""

    def __init__(self, max_samples: int = TREND_MAX_SAMPLES) -> None:
        self.max_samples = max_samples
        self._data: _List[_Tuple[float, float]] = []   # (monotonic_sec, value)

    def add(self, value: float, timestamp: float | None = None) -> None:
        """Record a sample. Timestamp defaults to time.monotonic()."""
        ts = _time.monotonic() if timestamp is None else timestamp
        self._data.append((ts, value))
        if len(self._data) > self.max_samples:
            self._data.pop(0)

    def predict_eta(self, threshold: float) -> float | None:
        """Minutes until threshold breach; None if <10 samples, flat, or descending."""
        if len(self._data) < 10:
            return None
        xs = [t - self._data[0][0] for t, _ in self._data]
        ys = [v for _, v in self._data]
        n = len(xs)
        sx = sum(xs); sy = sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if denom == 0:
            return None
        slope = (n * sxy - sx * sy) / denom
        if slope <= 0:
            return None                       # ← slope first (legacy order)
        latest = ys[-1]
        if threshold <= latest:
            return 0.0                        # ← breach second
        return (threshold - latest) / slope / 60.0

    def latest(self) -> float | None:
        return self._data[-1][1] if self._data else None

    def clear(self) -> None:
        self._data.clear()
```

**Tests**: `tests/unit/domain/watchdog/monitoring/test_trend_tracker.py` (6)

```python
from devforge.domain.watchdog.monitoring.tracker import TrendTracker


def test_insufficient_samples_none():
    t = TrendTracker()
    for i in range(9):
        t.add(float(i), float(i))
    assert t.predict_eta(100.0) is None


def test_flat_trend_none():
    t = TrendTracker()
    for i in range(15):
        t.add(50.0, float(i))
    assert t.predict_eta(100.0) is None


def test_descending_trend_none():
    t = TrendTracker()
    for i in range(15):
        t.add(100.0 - i * 5, float(i))
    assert t.predict_eta(90.0) is None


def test_ascending_predicts_minutes():
    t = TrendTracker()
    for i in range(15):
        t.add(50.0 + i, float(i * 60))       # +1/min
    eta = t.predict_eta(80.0)
    assert eta is not None and 25 <= eta <= 35


def test_breached_and_rising_returns_zero():
    t = TrendTracker()
    for i in range(15):
        t.add(100.0 + i, float(i * 60))
    assert t.predict_eta(90.0) == 0.0


def test_breached_but_descending_returns_none():
    """Parity guard: legacy checks slope first, so a breached-but-falling metric is None."""
    t = TrendTracker()
    for i in range(15):
        t.add(100.0 - i, float(i * 60))
    assert t.predict_eta(90.0) is None


def test_clear():
    t = TrendTracker()
    for i in range(15):
        t.add(float(i), float(i))
    t.clear()
    assert t.latest() is None
```

> v2.0 defect fixed: check order (slope→breach), window 60, seconds-based timestamps. `test_clear` added (7 tests here includes the parity guard).

Commit: `feat(phase2): add TrendTracker with legacy-parity ETA`

---

## Task A5: State Persistence (`ports/state_persistence.py` + `storage/state_json.py`)

Legacy: `state.py:290-336`, `config.py:34`. **Parity-critical**: payload keys are `components` (list), `last_heartbeat_ts`, `mode`.

```python
# src/devforge/ports/state_persistence.py
"""Port for watchdog state persistence (legacy watchdog_state.json)."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Protocol


class StateStoragePort(Protocol):
    """Persist/restore the legacy watchdog state payload."""

    def save(self, trackers: Mapping[str, Any], last_heartbeat_ts: float, mode: str) -> bool: ...

    def load(self) -> Dict[str, Any]:
        """Return {"components": [dict, ...], "last_heartbeat_ts": float, "mode": str}."""
        ...
```

```python
# src/devforge/adapters/driven/storage/state_json.py
"""Atomic JSON persistence, legacy-compatible (state.py:290-336)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from devforge.ports.state_persistence import StateStoragePort

log = logging.getLogger(__name__)
DEFAULT_STATE_FILE = "/opt/ai_data/scripts/watchdog_state.json"


class JsonStateStorage(StateStoragePort):
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or os.environ.get("WATCHDOG_STATE_FILE", DEFAULT_STATE_FILE))

    def save(self, trackers: Mapping[str, Any], last_heartbeat_ts: float, mode: str) -> bool:
        try:
            payload = {  # exact legacy shape (state.py:300-304)
                "components": [t.to_dict() for t in trackers.values()],
                "last_heartbeat_ts": last_heartbeat_ts,
                "mode": mode,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                       prefix=".watchdog_state_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except Exception:
                os.unlink(tmp)
                raise
        except Exception as e:
            log.warning("Failed to save watchdog state: %s", e)
            return False

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception as e:
            log.warning("Failed to load watchdog state: %s", e)
            return {}
```

**Tests**: `tests/unit/adapters/driven/storage/test_state_json.py` (5)

```python
from devforge.adapters.driven.storage.state_json import JsonStateStorage
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker


def test_roundtrip_legacy_shape(tmp_path):
    storage = JsonStateStorage(path=str(tmp_path / "s.json"))
    t = ComponentTracker("svc:x"); t.record_failure(); t.record_failure()
    assert storage.save({"svc:x": t}, last_heartbeat_ts=123.0, mode="day")
    payload = storage.load()
    assert set(payload) == {"components", "last_heartbeat_ts", "mode"}
    assert payload["last_heartbeat_ts"] == 123.0 and payload["mode"] == "day"
    assert payload["components"][0]["consecutive_fail"] == 2


def test_atomic_no_tmp_left(tmp_path):
    storage = JsonStateStorage(path=str(tmp_path / "s.json"))
    storage.save({"svc:x": ComponentTracker("svc:x")}, 0.0, "day")
    assert list(tmp_path.glob(".watchdog_state_*.tmp")) == []


def test_missing_file_empty(tmp_path):
    assert JsonStateStorage(path=str(tmp_path / "nope.json")).load() == {}


def test_corrupt_file_empty(tmp_path):
    p = tmp_path / "c.json"; p.write_text("{bad")
    assert JsonStateStorage(path=str(p)).load() == {}


def test_legacy_file_is_readable(tmp_path):
    """Parity guard: a v1-format file must load (existing production file)."""
    p = tmp_path / "legacy.json"
    p.write_text('{"components": [{"name": "svc:x", "state": "UNHEALTHY", '
                 '"fail_count": 3, "consecutive_fail": 3}], '
                 '"last_heartbeat_ts": 9.0, "mode": "night"}')
    payload = JsonStateStorage(path=str(p)).load()
    assert payload["mode"] == "night" and payload["components"][0]["state"] == "UNHEALTHY"
```

> v2.0 defect fixed: payload now uses the legacy `{"components": [...], "last_heartbeat_ts", "mode"}` shape (was `{"version", "trackers"}`), `Optional[str]` typing, `logging` instead of `print`.

Commit: `feat(phase2): add legacy-compatible atomic state persistence`

---

## Phase A Summary

- Tasks: A1–A5. Commits: 5.
- Tests: 5 + 6 + 14 + 7 + 5 = **37**.
- Gate:
  ```bash
  pytest tests/unit/ports/test_types.py tests/unit/domain/watchdog/ \
         tests/unit/adapters/driven/storage/test_state_json.py -v   # 37 passed
  lint-imports                                                     # 4 KEPT
  ```

# Phase B: Recovery Domain Logic (Day 3, 3 Tasks, ~20 tests)

## Task B1: Recovery Port + Kind Classification

Legacy recovery is **component-kind dispatch** (`orchestrator.py`), wrapped by `graduated_recover` (`recovery.py:270-297`). There is no soft/medium/hard enum in production; the kind selects the `recover_*` command in the adapter.

```python
# src/devforge/ports/recovery.py
"""Recovery port (legacy recover_* family)."""
from __future__ import annotations

from typing import Protocol

from devforge.ports.types import RecoveryAction


class RecoveryPort(Protocol):
    async def execute_recovery(self, action: RecoveryAction) -> bool: ...
```

```python
# src/devforge/domain/watchdog/recovery/strategies.py
"""Recovery kind classification (orchestrator.py dispatch table)."""
from __future__ import annotations

from typing import Optional, Protocol

from devforge.ports.types import RecoveryAction

# Exact-match overrides take precedence over prefix rules.
_EXACT: dict[str, str] = {
    "svc:svc-pod-forwarding": "svcpod",   # recover_svcpod_forwarding
    "svc:ebook-watcher": "ebook",         # recover_ebook_watcher
    "system:memory": "oom",               # recover_oom
}
# Prefix → kind. "" prefix means "no recovery (alert-only)".
_PREFIX: dict[str, str] = {
    "oneshot:": "oneshot",                # recover_oneshot
    "timer:": "timer_kick",               # systemctl --user start <svc>
    "llm:": "cascade",                    # recover_inference_cascade
    "infra:": "cascade",
    "pipeline:": "pipeline",              # systemctl --user restart devforge-day-cycle
    "syssvc:": "",                        # alert-only (rootful, no restart)
    "system:disk": "",                    # alert-only
}


def classify_recovery_kind(component: str) -> Optional[str]:
    """Return the recovery kind for a tracker key, or None if alert-only."""
    if component in _EXACT:
        return _EXACT[component]
    if component.startswith("svc:container-"):
        return "container"                # recover_container
    if component.startswith("svc:"):
        return "service"                  # recover_service
    for prefix, kind in _PREFIX.items():
        if component.startswith(prefix):
            return kind or None
    return None


class RecoveryStrategy(Protocol):
    def kind_for(self, component: str) -> Optional[str]: ...
    def create_action(self, component: str, state: str, reason: str, backoff_sec: int) -> Optional[RecoveryAction]: ...


class DefaultRecoveryStrategy:
    """Maps a component key to a RecoveryAction using the legacy dispatch table."""

    def kind_for(self, component: str) -> Optional[str]:
        return classify_recovery_kind(component)

    def create_action(self, component: str, state: str, reason: str,
                      backoff_sec: int) -> Optional[RecoveryAction]:
        kind = self.kind_for(component)
        if kind is None:
            return None
        return RecoveryAction(component=component, kind=kind, reason=reason, backoff_sec=backoff_sec)
```

**Tests**: `tests/unit/domain/watchdog/recovery/test_strategies.py` (6)

```python
from devforge.domain.watchdog.recovery.strategies import classify_recovery_kind, DefaultRecoveryStrategy


def test_service_kind():
    assert classify_recovery_kind("svc:devforge-turn-watcher") == "service"


def test_container_kind():
    assert classify_recovery_kind("svc:container-devforge-fastapi") == "container"


def test_exact_overrides():
    assert classify_recovery_kind("svc:svc-pod-forwarding") == "svcpod"
    assert classify_recovery_kind("svc:ebook-watcher") == "ebook"
    assert classify_recovery_kind("system:memory") == "oom"


def test_prefix_kinds():
    assert classify_recovery_kind("timer:devforge-day-cycle.timer") == "timer_kick"
    assert classify_recovery_kind("llm:day-extract") == "cascade"
    assert classify_recovery_kind("oneshot:news-collector") == "oneshot"


def test_alert_only_returns_none():
    assert classify_recovery_kind("syssvc:caddy") is None
    assert classify_recovery_kind("system:disk:/") is None


def test_create_action_includes_backoff():
    a = DefaultRecoveryStrategy().create_action("svc:x", "UNHEALTHY", "inactive", 40)
    assert a is not None and a.kind == "service" and a.backoff_sec == 40
```

Commit: `feat(phase2): add recovery port and kind classification`

---

## Task B2: RecoveryCoordinator (`recovery/graduation.py`)

Ports `graduated_recover` (recovery.py:270-297): circuit gate → backoff → execute → probe → record. Domain stays pure (no sleeps/I/O); the application performs the backoff sleep and the adapter performs the probe.

```python
# src/devforge/domain/watchdog/recovery/graduation.py
"""Recovery coordinator — legacy graduated_recover (recovery.py:270-297)."""
from __future__ import annotations

from typing import Optional

from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.recovery.strategies import DefaultRecoveryStrategy, RecoveryStrategy
from devforge.ports.types import RecoveryAction


class RecoveryCoordinator:
    def __init__(self, registry: TrackerRegistry, strategy: Optional[RecoveryStrategy] = None) -> None:
        self._registry = registry
        self._strategy = strategy or DefaultRecoveryStrategy()

    def plan(self, component: str, reason: str) -> Optional[RecoveryAction]:
        """Return a RecoveryAction, or None if circuit OPEN / alert-only.

        Mirrors the `if not tracker.can_retry(): return False` gate at
        recovery.py:276-278 and the backoff lookup at recovery.py:280-282.
        """
        t = self._registry.get(component)
        if not t.can_retry():
            return None
        return self._strategy.create_action(component, t.state.value, reason, t.backoff_sec())

    def record_result(self, component: str, succeeded: bool) -> bool:
        """Record success/failure after recovery (recovery.py:288-294)."""
        t = self._registry.get(component)
        if succeeded:
            t.record_success()
            return False
        return t.record_failure()

    def should_escalate(self, component: str) -> bool:
        return self._registry.get(component).state.value == "DOWN"
```

**Tests**: `tests/unit/domain/watchdog/recovery/test_graduation.py` (8)

```python
from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator


def test_plan_returns_action():
    reg = TrackerRegistry()
    c = RecoveryCoordinator(reg)
    a = c.plan("svc:x", "inactive")
    assert a is not None and a.kind == "service"


def test_plan_none_for_alert_only():
    c = RecoveryCoordinator(TrackerRegistry())
    assert c.plan("syssvc:caddy", "down") is None


def test_plan_none_when_circuit_open():
    reg = TrackerRegistry()
    t = reg.get("svc:x")
    for _ in range(3):
        t.record_failure()
    assert RecoveryCoordinator(reg).plan("svc:x", "down") is None


def test_plan_includes_backoff():
    reg = TrackerRegistry()
    reg.get("svc:x").record_failure()          # consecutive=1 → 10s ±10%
    a = RecoveryCoordinator(reg).plan("svc:x", "inactive")
    assert a is not None and 9 <= a.backoff_sec <= 11


def test_record_success_resets():
    reg = TrackerRegistry()
    reg.get("svc:x").record_failure()
    RecoveryCoordinator(reg).record_result("svc:x", True)
    assert reg.get("svc:x").state.value == "HEALTHY"


def test_record_failure_advances():
    reg = TrackerRegistry()
    c = RecoveryCoordinator(reg)
    c.record_result("svc:x", False)
    assert reg.get("svc:x").state.value == "DEGRADED"


def test_should_escalate_on_down():
    reg = TrackerRegistry()
    c = RecoveryCoordinator(reg)
    for _ in range(5):
        c.record_result("svc:x", False)
    assert c.should_escalate("svc:x") is True


def test_custom_strategy_injection():
    class Stub:
        def kind_for(self, component): return "service"
        def create_action(self, component, state, reason, backoff_sec):
            from devforge.ports.types import RecoveryAction
            return RecoveryAction(component, "stub", reason, 0)
    a = RecoveryCoordinator(TrackerRegistry(), strategy=Stub()).plan("svc:x", "r")
    assert a is not None and a.kind == "stub"
```

Commit: `feat(phase2): add RecoveryCoordinator (graduated_recover)`

---

## Task B3: CheckCoordinator (`orchestration/check_coordinator.py`)

Aggregates batch health ports into per-component `HealthCheck` results and records them in the registry.

```python
# src/devforge/domain/watchdog/orchestration/check_coordinator.py
"""Check dispatch (orchestrator.py split)."""
from __future__ import annotations

from collections.abc import Mapping

from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck


class CheckCoordinator:
    def __init__(self, registry: TrackerRegistry,
                 health_ports: Mapping[str, HealthCheckPort]) -> None:
        self._registry = registry
        self._ports = health_ports

    async def run(self, groups: list[str] | None = None) -> list[HealthCheck]:
        """Run all (or the named) check groups; record results; return checks."""
        names = groups if groups is not None else list(self._ports)
        results: list[HealthCheck] = []
        for name in names:
            port = self._ports.get(name)
            if port is None:
                continue
            for check in await port.check_health():
                self._registry.get(check.component).record_check(check)
                results.append(check)
        return results

    @staticmethod
    def failed(checks: list[HealthCheck]) -> list[str]:
        return [c.component for c in checks if not c.is_healthy]

    @staticmethod
    def healthy(checks: list[HealthCheck]) -> list[str]:
        return [c.component for c in checks if c.is_healthy]
```

> This design resolves the v1 key-mismatch bug (plan iterated `critical_services` while ports were keyed by check type): here adapters emit namespaced component keys, so registry keys, recovery keys, and incident keys all agree.

**Tests**: `tests/unit/domain/watchdog/orchestration/test_check_coordinator.py` (6)

```python
import pytest
from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator
from devforge.ports.types import HealthCheck


class FakePort:
    def __init__(self, checks): self._checks = checks
    async def check_health(self): return list(self._checks)


@pytest.mark.asyncio
async def test_run_records_all():
    reg = TrackerRegistry()
    coord = CheckCoordinator(reg, {"svc": FakePort([
        HealthCheck(component="svc:a", is_healthy=True, detail="ok"),
        HealthCheck(component="svc:b", is_healthy=False, detail="down"),
    ])})
    checks = await coord.run()
    assert len(checks) == 2
    assert reg.get("svc:a").state.value == "HEALTHY"
    assert reg.get("svc:b").state.value == "DEGRADED"


@pytest.mark.asyncio
async def test_run_subset_groups():
    coord = CheckCoordinator(TrackerRegistry(), {
        "a": FakePort([HealthCheck(component="svc:a", is_healthy=True, detail="")]),
        "b": FakePort([HealthCheck(component="svc:b", is_healthy=True, detail="")]),
    })
    checks = await coord.run(groups=["a"])
    assert [c.component for c in checks] == ["svc:a"]


@pytest.mark.asyncio
async def test_unknown_group_skipped():
    coord = CheckCoordinator(TrackerRegistry(), {})
    assert await coord.run(groups=["missing"]) == []


def test_failed_filter():
    checks = [HealthCheck("svc:a", True, ""), HealthCheck("svc:b", False, "")]
    assert CheckCoordinator.failed(checks) == ["svc:b"]


def test_healthy_filter():
    checks = [HealthCheck("svc:a", True, ""), HealthCheck("svc:b", False, "")]
    assert CheckCoordinator.healthy(checks) == ["svc:a"]


@pytest.mark.asyncio
async def test_empty_port_list():
    coord = CheckCoordinator(TrackerRegistry(), {"svc": FakePort([])})
    assert await coord.run() == []
```

Commit: `feat(phase2): add CheckCoordinator batch dispatch`

---

# Phase C: Health Check Adapters (Day 4, 4 Tasks, ~20 tests + 4 parity)

All adapters implement `HealthCheckPort` and return `list[HealthCheck]` with **namespaced component keys**.

```python
# src/devforge/ports/health_check.py
"""Health check port (batch)."""
from __future__ import annotations

from typing import Protocol

from devforge.ports.types import HealthCheck


class HealthCheckPort(Protocol):
    async def check_health(self) -> list[HealthCheck]: ...
```

## Task C1: systemd Health (`health/systemd_health.py`)

Legacy: `checker.py:150-162` (`check_service` → `systemctl --user is-active`), `checker.py:371-397` (`check_timer` → `LastTriggerUSec`).

```python
# src/devforge/adapters/driven/health/systemd_health.py
"""systemd user service/timer checks (legacy checker.py:150-162, 371-397)."""
from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from typing import Iterable, Mapping

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

DEFAULT_TIMER_MAX_IDLE_SEC = 2100


async def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=5)


class SystemdServiceHealthChecker(HealthCheckPort):
    def __init__(self, services: Iterable[str], prefix: str = "svc") -> None:
        self._services = list(services)
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name) for name in self._services]

    async def _check(self, name: str) -> HealthCheck:
        try:
            r = await _run(["systemctl", "--user", "is-active", name])
            ok = r.returncode == 0
            detail = "active" if ok else "inactive"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        return HealthCheck(component=f"{self._prefix}:{name}", is_healthy=ok, detail=detail)


class SystemdTimerHealthChecker(HealthCheckPort):
    def __init__(self, timers: Mapping[str, int], prefix: str = "timer") -> None:
        self._timers = dict(timers)   # timer unit -> max_idle_sec
        self._prefix = prefix

    async def check_health(self) -> list[HealthCheck]:
        return [await self._check(name, max_idle) for name, max_idle in self._timers.items()]

    async def _check(self, name: str, max_idle: int) -> HealthCheck:
        try:
            r = await _run(["systemctl", "--user", "show", name,
                            "--property=LastTriggerUSec", "--value"])
            last = r.stdout.strip()
            if not last or last == "n/a":
                return HealthCheck(f"{self._prefix}:{name}", False, "never triggered")
            last_dt = datetime.strptime(last, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            idle = (datetime.now(timezone.utc) - last_dt).total_seconds()
            ok = idle <= max_idle
            detail = f"{int(idle)}s ago" if ok else f"{int(idle)}s idle > {max_idle}s limit"
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        return HealthCheck(f"{self._prefix}:{name}", ok, detail)
```

**Tests**: `tests/unit/adapters/driven/health/test_systemd_health.py` (5)

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from devforge.adapters.driven.health.systemd_health import (
    SystemdServiceHealthChecker, SystemdTimerHealthChecker)


@pytest.mark.asyncio
async def test_all_services_active():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        checks = await SystemdServiceHealthChecker(["a", "b"]).check_health()
    assert all(c.is_healthy for c in checks)
    assert [c.component for c in checks] == ["svc:a", "svc:b"]


@pytest.mark.asyncio
async def test_inactive_service():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=3))):
        checks = await SystemdServiceHealthChecker(["a"]).check_health()
    assert checks[0].is_healthy is False and "inactive" in checks[0].detail


@pytest.mark.asyncio
async def test_prefix_override():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        checks = await SystemdServiceHealthChecker(["x"], prefix="container").check_health()
    assert checks[0].component == "container:x"


@pytest.mark.asyncio
async def test_timer_never_triggered():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout="n/a\n"))):
        checks = await SystemdTimerHealthChecker({"t.timer": 100}).check_health()
    assert checks[0].is_healthy is False and checks[0].component == "timer:t.timer"


@pytest.mark.asyncio
async def test_timer_recent_ok():
    recent = "Mon 2026-09-22 00:00:00 UTC"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=recent + "\n"))):
        with patch("devforge.adapters.driven.health.systemd_health.datetime") as dt:
            dt.strptime.side_effect = __import__("datetime").datetime.strptime
            dt.now.return_value = __import__("datetime").datetime(2026, 9, 22, 0, 1, tzinfo=__import__("datetime").timezone.utc)
            checks = await SystemdTimerHealthChecker({"t.timer": 2100}).check_health()
    assert checks[0].is_healthy is True
```

**Parity**: `tests/characterization/test_systemd_parity.py` (1) — compare `check_health()` against live `systemctl --user is-active` for `devforge-turn-watcher` (skipped if unit absent).

Commit: `feat(phase2): add systemd service/timer health adapters`

---

## Task C2: LLM Health (`health/llm_health.py`)

Legacy: `checker.py:79-127` (T1 `GET /health`, T2 `POST /v1/chat/completions`), `checker.py:493-528` (`check_all_llm`, day-port gating, transient skip).

```python
# src/devforge/adapters/driven/health/llm_health.py
"""LLM inference probes (legacy checker.py:79-127, 493-528)."""
from __future__ import annotations

from typing import Callable, Mapping, Optional

import httpx

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

TRANSIENT_TOKENS = ("503", "loading model")


class LLMHealthChecker(HealthCheckPort):
    def __init__(self, targets: Mapping[str, int], timeout: int = 60,
                 day_ports: Optional[set[int]] = None,
                 mode_reader: Optional[Callable[[], str]] = None) -> None:
        self._targets = dict(targets)          # label -> port
        self._timeout = timeout
        self._day_ports = day_ports or set()
        self._mode_reader = mode_reader or (lambda: "day")

    async def check_health(self) -> list[HealthCheck]:
        mode = self._mode_reader()
        checks: list[HealthCheck] = []
        for label, port in self._targets.items():
            if mode == "day" and self._day_ports and port not in self._day_ports:
                continue                        # night-only port, skip in day
            checks.append(await self._probe(label, port))
        return checks

    async def _probe(self, label: str, port: int) -> HealthCheck:
        component = f"llm:{label}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                t1 = await client.get(f"http://127.0.0.1:{port}/health")
                if t1.status_code != 200:
                    detail = f"HTTP {t1.status_code}"
                    return self._result(component, False, detail)
                t2 = await client.post(
                    f"http://127.0.0.1:{port}/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 1, "temperature": 0.1, "stream": False},
                )
                if t2.status_code == 200 and t2.json().get("choices"):
                    return self._result(component, True, "probe ok")
                return self._result(component, False, "bad probe response")
        except Exception as e:  # noqa: BLE001
            detail = str(e)
            if any(tok in detail.lower() for tok in TRANSIENT_TOKENS):
                return self._result(component, True, f"transient: {detail}")  # not a fault
            return self._result(component, False, detail)

    @staticmethod
    def _result(component: str, ok: bool, detail: str) -> HealthCheck:
        return HealthCheck(component=component, is_healthy=ok, detail=detail)
```

**Tests**: `tests/unit/adapters/driven/health/test_llm_health.py` (5)

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from devforge.adapters.driven.health.llm_health import LLMHealthChecker


def _client(get_status=200, post_status=200, post_json=None, get_exc=None, post_exc=None):
    c = MagicMock()
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=False)
    c.get = AsyncMock(side_effect=get_exc, return_value=MagicMock(status_code=get_status))
    if post_json is None:
        post_json = {"choices": [{"message": {"content": ""}}]}
    c.post = AsyncMock(side_effect=post_exc, return_value=MagicMock(status_code=post_status, json=lambda: post_json))
    return c


@pytest.mark.asyncio
async def test_probe_ok():
    with patch("httpx.AsyncClient", return_value=_client()):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy and checks[0].component == "llm:day-extract"


@pytest.mark.asyncio
async def test_t1_failure():
    with patch("httpx.AsyncClient", return_value=_client(get_status=500)):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_transient_503_is_healthy():
    with patch("httpx.AsyncClient", return_value=_client(get_exc=Exception("HTTP 503 loading model"))):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is True and "transient" in checks[0].detail


@pytest.mark.asyncio
async def test_night_port_skipped_in_day():
    c = _client()
    with patch("httpx.AsyncClient", return_value=c):
        checks = await LLMHealthChecker({"night-verify": 8084}, day_ports={8082}).check_health()
    assert checks == []


@pytest.mark.asyncio
async def test_connection_refused_unhealthy():
    with patch("httpx.AsyncClient", return_value=_client(get_exc=Exception("connection refused"))):
        checks = await LLMHealthChecker({"day-extract": 8082}).check_health()
    assert checks[0].is_healthy is False
```

**Parity**: `tests/characterization/test_llm_parity.py` (1) — probe the currently serving port and compare with `checker.check_health` (skip if no model loaded).

Commit: `feat(phase2): add LLM health adapter`

---

## Task C3: System Health (`health/system_health.py`)

Legacy: `checker.py:306-343` (`check_memory`, `free -m`), `checker.py:343-371` (`check_disk`, `df -h`).

```python
# src/devforge/adapters/driven/health/system_health.py
"""Memory/disk checks (legacy checker.py:306-371)."""
from __future__ import annotations

import asyncio
import subprocess
from typing import Iterable

from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

MEM_WARN_PCT = 90
MEM_CRIT_PCT = 95
SWAP_CRIT_MB = 1024


async def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True, timeout=5)


class MemoryHealthChecker(HealthCheckPort):
    async def check_health(self) -> list[HealthCheck]:
        try:
            r = await _run(["free", "-m"])
            pct = swap_pct = 0
            swap_used = 0
            for line in r.stdout.splitlines():
                parts = line.split()
                if line.startswith("Mem:"):
                    total, used = int(parts[1]), int(parts[2])
                    pct = round(used / total * 100) if total else 0
                elif line.startswith("Swap:"):
                    total, used = int(parts[1]), int(parts[2])
                    swap_used = used
                    swap_pct = round(used / total * 100) if total else 0
            ok = pct < MEM_CRIT_PCT and swap_used < SWAP_CRIT_MB
            detail = f"mem={pct}% swap={swap_pct}%"
            return [HealthCheck("system:memory", ok, detail, metric_value=float(pct), threshold=float(MEM_CRIT_PCT))]
        except Exception as e:  # noqa: BLE001
            return [HealthCheck("system:memory", False, str(e))]


class DiskHealthChecker(HealthCheckPort):
    def __init__(self, mounts: Iterable[str], threshold_pct: int = 90) -> None:
        self._mounts = list(mounts)
        self._threshold = threshold_pct

    async def check_health(self) -> list[HealthCheck]:
        try:
            r = await _run(["df", "--output=target,pcent", "-x", "tmpfs"])
            usage: dict[str, int] = {}
            for line in r.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    usage[parts[0]] = int(parts[1].replace("%", ""))
            return [self._one(m, usage.get(m)) for m in self._mounts]
        except Exception as e:  # noqa: BLE001
            return [HealthCheck(f"system:disk:{m}", False, str(e)) for m in self._mounts]

    def _one(self, mount: str, pct: int | None) -> HealthCheck:
        if pct is None:
            return HealthCheck(f"system:disk:{mount}", False, "mount not found")
        ok = pct < self._threshold
        return HealthCheck(f"system:disk:{mount}", ok, f"{pct}% used",
                           metric_value=float(pct), threshold=float(self._threshold))
```

**Tests**: `tests/unit/adapters/driven/health/test_system_health.py` (5)

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from devforge.adapters.driven.health.system_health import MemoryHealthChecker, DiskHealthChecker, MEM_CRIT_PCT


@pytest.mark.asyncio
async def test_memory_ok():
    out = "              total        used\nMem:          16000        8000\nSwap:          4096         100\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await MemoryHealthChecker().check_health()
    assert checks[0].is_healthy and checks[0].component == "system:memory"


@pytest.mark.asyncio
async def test_memory_crit():
    out = "              total        used\nMem:          16000       15800\nSwap:          4096        2048\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await MemoryHealthChecker().check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_disk_ok():
    out = "Target  Use%\n/        40%\n/opt     50%\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await DiskHealthChecker(["/", "/opt"]).check_health()
    assert all(c.is_healthy for c in checks)
    assert {c.component for c in checks} == {"system:disk:/", "system:disk:/opt"}


@pytest.mark.asyncio
async def test_disk_high():
    out = "Target  Use%\n/        95%\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await DiskHealthChecker(["/"]).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_disk_mount_missing():
    out = "Target  Use%\n/        40%\n"
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(stdout=out))):
        checks = await DiskHealthChecker(["/opt/ai_data"]).check_health()
    assert checks[0].is_healthy is False and "not found" in checks[0].detail
```

**Parity**: `tests/characterization/test_system_parity.py` (1) — memory pct from `free -m` vs adapter.

Commit: `feat(phase2): add memory/disk health adapters`

---

## Task C4: Heartbeat Health (`health/pipeline_health.py`)

Legacy: `checker.py:417-491` (`check_heartbeats`), `messenger.py:155-186` (`check_heartbeat`), `config.py:145` (`HEARTBEAT_WORKERS`).

```python
# src/devforge/adapters/driven/health/pipeline_health.py
"""Worker heartbeat checks (legacy checker.py:417-491)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping

from sqlalchemy import text

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

DEFAULT_WORKER_MAX_AGE = 1800


class HeartbeatHealthChecker(HealthCheckPort):
    """Dead-man's switch for heartbeat_* rows in watchdog_pulses."""

    def __init__(self, gateway: DatabaseGateway,
                 workers: Mapping[str, int] | None = None) -> None:
        self._gateway = gateway
        self._workers = dict(workers or {})   # worker -> max_age_sec

    async def check_health(self) -> list[HealthCheck]:
        try:
            async with self._gateway.session() as session:
                result = await session.execute(text(
                    "SELECT pulse_id, status, "
                    "EXTRACT(EPOCH FROM (now() - created_at))::int AS age "
                    "FROM watchdog_pulses WHERE pulse_id LIKE 'heartbeat\\_%' ESCAPE '\\'"
                ))
                rows = list(result.mappings())
        except Exception as e:  # noqa: BLE001
            return [HealthCheck("heartbeat:db", False, f"query failed: {e}")]

        seen = {r["pulse_id"].removeprefix("heartbeat_"): r for r in rows}
        checks: list[HealthCheck] = []
        for worker, max_age in self._workers.items():
            row = seen.get(worker)
            if row is None:
                checks.append(HealthCheck(f"heartbeat:{worker}", False, "never beat"))
                continue
            if row["status"] in ("RESOLVED", "IGNORED"):
                checks.append(HealthCheck(f"heartbeat:{worker}", True, row["status"]))
                continue
            age = int(row["age"] or 0)
            checks.append(HealthCheck(f"heartbeat:{worker}", age < max_age,
                                      f"{age}s ago" if age < max_age else f"{age}s >= {max_age}s"))
        return checks
```

**Tests**: `tests/unit/adapters/driven/health/test_pipeline_health.py` (5)

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest
from devforge.adapters.driven.health.pipeline_health import HeartbeatHealthChecker


def _gateway(rows):
    result = MagicMock(); result.mappings.return_value = rows
    session = MagicMock(); session.execute = AsyncMock(return_value=result)
    cm = MagicMock(); cm.__aenter__ = AsyncMock(return_value=session); cm.__aexit__ = AsyncMock(return_value=False)
    gw = MagicMock(); gw.session.return_value = cm
    return gw


@pytest.mark.asyncio
async def test_fresh_heartbeat():
    gw = _gateway([{"pulse_id": "heartbeat_day_extract", "status": "IN_PROGRESS", "age": 100}])
    checks = await HeartbeatHealthChecker(gw, {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy and checks[0].component == "heartbeat:day_extract"


@pytest.mark.asyncio
async def test_stale_heartbeat():
    gw = _gateway([{"pulse_id": "heartbeat_day_extract", "status": "IN_PROGRESS", "age": 3000}])
    checks = await HeartbeatHealthChecker(gw, {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy is False


@pytest.mark.asyncio
async def test_never_beat():
    checks = await HeartbeatHealthChecker(_gateway([]), {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy is False and "never" in checks[0].detail


@pytest.mark.asyncio
async def test_resolved_counts_alive():
    gw = _gateway([{"pulse_id": "heartbeat_news_collector", "status": "RESOLVED", "age": 99999}])
    checks = await HeartbeatHealthChecker(gw, {"news_collector": 1800}).check_health()
    assert checks[0].is_healthy is True


@pytest.mark.asyncio
async def test_db_error_unhealthy():
    gw = MagicMock(); gw.session.side_effect = RuntimeError("db down")
    checks = await HeartbeatHealthChecker(gw, {"day_extract": 1800}).check_health()
    assert checks[0].is_healthy is False and "query failed" in checks[0].detail
```

**Parity**: `tests/characterization/test_heartbeat_parity.py` (1) — compare against `messenger.check_heartbeat` for one worker (skipped if DB unreachable from host).

Commit: `feat(phase2): add heartbeat health adapter`

---

## Phase C Summary

- Tasks C1–C4. Commits: 4.
- Tests: 5 + 5 + 5 + 5 = **20 unit** + 4 parity.

# Phase D: Notification & Recovery Adapters (Day 5, 3 Tasks, ~11 tests)

```python
# src/devforge/ports/notification.py
"""Notification port (legacy notifier.py:345, 362)."""
from __future__ import annotations

from typing import Protocol


class NotificationPort(Protocol):
    async def send_alert(self, component: str, state: str, detail: str) -> bool: ...
    async def send_recovery(self, component: str, detail: str) -> bool: ...
    async def sd_notify(self, state: str) -> bool: ...
```

## Task D1: Slack (`notification/slack_notifier.py`)

Legacy: `notifier.py:60-81` (`_slack_api`), `:345` (`send_alert`), `:362` (`send_recovery`). Slack **Web API + bot token**, not a webhook.

```python
# src/devforge/adapters/driven/notification/slack_notifier.py
"""Slack Web API notifier (legacy notifier.py:60-81, 345, 362)."""
from __future__ import annotations

import logging

import httpx

from devforge.ports.notification import NotificationPort

log = logging.getLogger(__name__)
SLACK_API = "https://slack.com/api"


class SlackNotifier(NotificationPort):
    def __init__(self, token: str, channel: str, timeout: int = 10) -> None:
        self._token = token
        self._channel = channel
        self._timeout = timeout

    async def _post(self, method: str, payload: dict) -> bool:
        payload.setdefault("channel", self._channel)
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(f"{SLACK_API}/{method}", json=payload,
                                      headers={"Authorization": f"Bearer {self._token}"})
                ok = bool(r.json().get("ok"))
                if not ok:
                    log.warning("slack %s not ok: %s", method, r.text[:200])
                return ok
        except Exception as e:  # noqa: BLE001
            log.warning("slack %s failed: %s", method, e)
            return False

    async def send_alert(self, component: str, state: str, detail: str) -> bool:
        return await self._post("chat.postMessage", {
            "text": f":rotating_light: {component} → {state}: {detail}",
        })

    async def send_recovery(self, component: str, detail: str) -> bool:
        return await self._post("chat.postMessage", {
            "text": f":white_check_mark: {component} recovered: {detail}",
        })

    async def sd_notify(self, state: str) -> bool:
        return False  # Slack does not implement sd_notify
```

**Tests**: `tests/unit/adapters/driven/notification/test_slack_notifier.py` (4)

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from devforge.adapters.driven.notification.slack_notifier import SlackNotifier


def _client(json_ok=True):
    c = MagicMock(); c.__aenter__ = AsyncMock(return_value=c); c.__aexit__ = AsyncMock(return_value=False)
    c.post = AsyncMock(return_value=MagicMock(json=lambda: {"ok": json_ok}, text=""))
    return c


@pytest.mark.asyncio
async def test_send_alert_ok():
    with patch("httpx.AsyncClient", return_value=_client()):
        assert await SlackNotifier("tok", "#chan").send_alert("svc:x", "UNHEALTHY", "inactive") is True


@pytest.mark.asyncio
async def test_send_alert_api_error():
    with patch("httpx.AsyncClient", return_value=_client(json_ok=False)):
        assert await SlackNotifier("tok", "#chan").send_alert("svc:x", "DOWN", "down") is False


@pytest.mark.asyncio
async def test_send_recovery_ok():
    with patch("httpx.AsyncClient", return_value=_client()):
        assert await SlackNotifier("tok", "#chan").send_recovery("svc:x", "restarted") is True


@pytest.mark.asyncio
async def test_network_error_returns_false():
    c = _client(); c.post = AsyncMock(side_effect=RuntimeError("no net"))
    with patch("httpx.AsyncClient", return_value=c):
        assert await SlackNotifier("tok", "#chan").send_alert("svc:x", "DOWN", "d") is False
```

Commit: `feat(phase2): add Slack Web API notifier`

---

## Task D2: systemd notify (`notification/systemd_notifier.py`)

Legacy: `notifier.py:26-48` (`sd_notify`).

```python
# src/devforge/adapters/driven/notification/systemd_notifier.py
"""sd_notify notifier (legacy notifier.py:26-48)."""
from __future__ import annotations

import asyncio
import subprocess

from devforge.ports.notification import NotificationPort


class SystemdNotifier(NotificationPort):
    async def sd_notify(self, state: str) -> bool:
        try:
            r = await asyncio.to_thread(subprocess.run, ["systemd-notify", "--user", state],
                                        capture_output=True, timeout=5)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def send_alert(self, component: str, state: str, detail: str) -> bool:
        return await self.sd_notify(f"STATUS=ALERT {component} {state} {detail}")

    async def send_recovery(self, component: str, detail: str) -> bool:
        return await self.sd_notify(f"STATUS=RECOVERED {component} {detail}")
```

**Tests**: `tests/unit/adapters/driven/notification/test_systemd_notifier.py` (2)

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from devforge.adapters.driven.notification.systemd_notifier import SystemdNotifier


@pytest.mark.asyncio
async def test_sd_notify_ok():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        assert await SystemdNotifier().sd_notify("STATUS=ready") is True


@pytest.mark.asyncio
async def test_send_alert_builds_status():
    calls = []
    async def fake(cmd, **kw):
        calls.append(cmd); return MagicMock(returncode=0)
    with patch("asyncio.to_thread", new=fake):
        ok = await SystemdNotifier().send_alert("svc:x", "DOWN", "inactive")
    assert ok and "STATUS=ALERT svc:x DOWN inactive" in calls[0]
```

Commit: `feat(phase2): add systemd sd_notify notifier`

---

## Task D3: systemd Recovery (`recovery/systemd_recovery.py`)

Legacy: `recovery.py:96-124` (`recover_service` = restart + sleep + health check), `:57-94` (`recover_container`), `:160-225` (oneshot/ebook/oom).

```python
# src/devforge/adapters/driven/recovery/systemd_recovery.py
"""RecoveryPort implementation (legacy recover_* family)."""
from __future__ import annotations

import asyncio
import subprocess
from typing import Callable, Optional

from devforge.ports.recovery import RecoveryPort
from devforge.ports.types import RecoveryAction

_RESTART = ("service", "container")
_PROBE_DELAY_SEC = 5.0   # legacy recovery.py:111 sleep(5)


class SystemdRecoveryAdapter(RecoveryPort):
    """Executes a RecoveryAction via systemctl; optional post-restart probe."""

    def __init__(self, probe: Optional[Callable[[str], "asyncio.Future[bool]"]] = None) -> None:
        self._probe = probe

    async def _systemctl(self, *args: str) -> bool:
        try:
            r = await asyncio.to_thread(subprocess.run, ["systemctl", "--user", *args],
                                        capture_output=True, timeout=30)
            return r.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    async def execute_recovery(self, action: RecoveryAction) -> bool:
        unit = action.component.split(":", 1)[1] if ":" in action.component else action.component
        if action.kind in _RESTART:
            ok = await self._systemctl("restart", unit)
        elif action.kind == "timer_kick":
            ok = await self._systemctl("start", unit.replace(".timer", ".service"))
        elif action.kind == "oneshot":
            ok = await self._systemctl("start", unit)
        elif action.kind == "svcpod":
            ok = await self._systemctl("restart", "svc-pod.service")
        elif action.kind == "pipeline":
            ok = await self._systemctl("restart", "devforge-day-cycle.service")
        elif action.kind == "cascade":
            ok = await self._systemctl("restart", "svc-pod.service")
        else:
            return False

        if ok and self._probe is not None:
            await asyncio.sleep(_PROBE_DELAY_SEC)
            try:
                ok = bool(await self._probe(action.component))
            except Exception:  # noqa: BLE001
                ok = False
        return ok
```

> Note: `cascade` maps to a restart of `svc-pod.service` here; the full legacy `recover_inference_cascade` (multiple escalation levels, `recovery.py:410-463`) is a Phase 3 refinement. The guide preserves the *contract* (bool result, probe-after-restart); deeper cascade logic is called out in Risk 2.

**Tests**: `tests/unit/adapters/driven/recovery/test_systemd_recovery.py` (5)

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
from devforge.ports.types import RecoveryAction


@pytest.mark.asyncio
async def test_restart_service():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        ok = await SystemdRecoveryAdapter().execute_recovery(RecoveryAction("svc:x", "service", "down"))
    assert ok is True


@pytest.mark.asyncio
async def test_restart_failure():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=1))):
        ok = await SystemdRecoveryAdapter().execute_recovery(RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_timer_kick_starts_service():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))) as t:
        await SystemdRecoveryAdapter().execute_recovery(
            RecoveryAction("timer:devforge-day-cycle.timer", "timer_kick", "delay"))
    assert "devforge-day-cycle.service" in t.call_args[0][0]


@pytest.mark.asyncio
async def test_probe_failure_overrides_ok():
    with patch("asyncio.to_thread", new=AsyncMock(return_value=MagicMock(returncode=0))):
        with patch("asyncio.sleep", new=AsyncMock()):
            probe = AsyncMock(return_value=False)
            ok = await SystemdRecoveryAdapter(probe=probe).execute_recovery(
                RecoveryAction("svc:x", "service", "down"))
    assert ok is False


@pytest.mark.asyncio
async def test_unknown_kind_false():
    assert await SystemdRecoveryAdapter().execute_recovery(
        RecoveryAction("svc:x", "unknown", "r")) is False
```

**Parity**: `tests/characterization/test_recovery_parity.py` (1) — `systemctl --user restart` on a throwaway unit vs adapter (skipped without a disposable unit).

Commit: `feat(phase2): add systemd recovery adapter`

---

# Phase E: Application & CLI (Day 6, 3 Tasks, ~15 tests)

```python
# src/devforge/ports/incident_repository.py
"""Incident repository port (legacy incidents.py)."""
from __future__ import annotations

from typing import Optional, Protocol

from devforge.ports.types import Incident


class IncidentRepository(Protocol):
    async def record_detect(self, component: str, event_type: str, detail: str,
                            unit: Optional[str] = None) -> Optional[int]: ...
    async def record_action(self, incident_id: Optional[int], action: str, ok: bool) -> None: ...
    async def resolve_if_open(self, component: str) -> None: ...
    async def find_open(self, component: Optional[str] = None) -> list[Incident]: ...
```

## Task E1: Incident Repository (`storage/incident_pg.py`)

**Schema SSOT**: `domain/models.py` `WatchdogIncident` ORM (matches the live table). Reuse it. Legacy dedup/reopen logic: `incidents.py:112-227`.

```python
# src/devforge/adapters/driven/storage/incident_pg.py
"""Postgres incidents (legacy incidents.py:112-227)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, update

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.domain.models import WatchdogIncident
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.types import Incident

REOPEN_WINDOW_SEC = 3600


def _to_incident(row: WatchdogIncident) -> Incident:
    return Incident(id=row.id, dedup_key=row.dedup_key, component=row.component, status=row.status,
                    symptom=row.symptom, context=row.context, detected_at=row.detected_at,
                    last_seen_at=row.last_seen_at, action=row.action, action_result=row.action_result,
                    action_at=row.action_at, resolved_at=row.resolved_at,
                    fail_count=row.fail_count, reopen_count=row.reopen_count)


class PostgresIncidentRepository(IncidentRepository):
    def __init__(self, gateway: DatabaseGateway) -> None:
        self._gateway = gateway

    async def record_detect(self, component: str, event_type: str, detail: str,
                            unit: Optional[str] = None) -> Optional[int]:
        dedup = f"{component}:{event_type}"
        async with self._gateway.session() as session:
            open_row = (await session.execute(
                select(WatchdogIncident).where(WatchdogIncident.dedup_key == dedup,
                                               WatchdogIncident.status == "open")
                .order_by(WatchdogIncident.id.desc()).limit(1))).scalar_one_or_none()
            if open_row is not None:
                await session.execute(update(WatchdogIncident).where(WatchdogIncident.id == open_row.id)
                                      .values(fail_count=WatchdogIncident.fail_count + 1,
                                              symptom=detail[:500], last_seen_at=func.now()))
                return open_row.id
            recent = (await session.execute(
                select(WatchdogIncident).where(WatchdogIncident.dedup_key == dedup,
                                               WatchdogIncident.status == "resolved",
                                               WatchdogIncident.resolved_at > func.now() - func.make_interval(secs=REOPEN_WINDOW_SEC))
                .order_by(WatchdogIncident.id.desc()).limit(1))).scalar_one_or_none()
            if recent is not None:
                await session.execute(update(WatchdogIncident).where(WatchdogIncident.id == recent.id)
                                      .values(status="open", reopen_count=WatchdogIncident.reopen_count + 1,
                                              fail_count=WatchdogIncident.fail_count + 1, resolved_at=None,
                                              detected_at=func.now(), last_seen_at=func.now(),
                                              symptom=detail[:500]))
                return recent.id
            new_row = (await session.execute(
                WatchdogIncident.__table__.insert()
                .values(dedup_key=dedup, component=component, status="open",
                        symptom=detail[:500], context=_capture_context(unit))
                .returning(WatchdogIncident.id))).scalar_one()
            return new_row

    async def record_action(self, incident_id: Optional[int], action: str, ok: bool) -> None:
        if incident_id is None:
            return
        values = {"action": action, "action_result": "success" if ok else "fail", "action_at": func.now()}
        if ok:
            values.update(status="resolved", resolved_at=func.now())
        async with self._gateway.session() as session:
            await session.execute(update(WatchdogIncident).where(WatchdogIncident.id == incident_id).values(**values))

    async def resolve_if_open(self, component: str) -> None:
        async with self._gateway.session() as session:
            await session.execute(update(WatchdogIncident)
                                  .where(WatchdogIncident.dedup_key.like(f"{component}:%"),
                                         WatchdogIncident.status == "open")
                                  .values(status="resolved", resolved_at=func.now(),
                                          action=func.coalesce(WatchdogIncident.action, "auto-recovered"),
                                          action_result="success", action_at=func.now()))

    async def find_open(self, component: Optional[str] = None) -> list[Incident]:
        stmt = select(WatchdogIncident).where(WatchdogIncident.status == "open")
        if component is not None:
            stmt = stmt.where(WatchdogIncident.component == component)
        async with self._gateway.session() as session:
            rows = (await session.execute(stmt.order_by(WatchdogIncident.detected_at.desc()))).scalars().all()
        return [_to_incident(r) for r in rows]


def _capture_context(unit: Optional[str]) -> Optional[str]:
    """Best-effort bounded, masked diagnostic context (legacy incidents.py:87-109)."""
    return None  # Phase 3: implement podman/journalctl capture + mask_secrets
```

**Tests**: `tests/unit/adapters/driven/storage/test_incident_pg.py` (5) — fake gateway/session, assert dedup/reopen/resolve SQL flow and ORM→Incident mapping. **Parity**: assert the ORM column set equals the live `watchdog_incidents` columns (`\d watchdog_incidents`).

Commit: `feat(phase2): add Postgres incident repository`

---

## Task E2: WatchdogConfig + Application Service (`application/watchdog_service.py`)

### E2a — `WatchdogConfig` (`core/config.py`, extend)

Defaults mirror `scripts/lib/watchdog/config.py` (SERVICE_TARGETS, TIMER_TARGETS, LLM_TARGETS, HEARTBEAT_WORKERS).

```python
@dataclass(frozen=True)
class WatchdogConfig:
    failure_threshold: int = 3
    success_threshold: int = 2               # reserved; circuit is 3/120s/5 in legacy
    circuit_reset_timeout_sec: int = 120
    backoff_reset_sec: int = 600
    check_interval_sec: int = 60
    check_timeout_sec: int = 30
    state_file: str = "/opt/ai_data/scripts/watchdog_state.json"

    critical_services: list[str] = field(default_factory=lambda: [
        "devforge-turn-watcher", "openrouter-rr-proxy", "devforge-day-cycle",
        "ebook-watcher", "container-devforge-fastapi", "container-devforge-worker",
        "ebook-api", "devforge-news-api", "cashbook",
    ])
    timers: dict[str, int] = field(default_factory=lambda: {
        "devforge-day-cycle.timer": 2100, "devforge-night-cycle.timer": 2100,
    })
    llm_targets: dict[str, int] = field(default_factory=lambda: {"day-extract": 8082, "night-verify": 8084})
    day_ports: list[int] = field(default_factory=lambda: [8080, 8082])
    heartbeat_workers: dict[str, int] = field(default_factory=lambda: {
        "embed_batch": 1800, "liveness_embed_batch": 1800, "entity_scan": 1800,
        "text_clean": 1800, "day_extract": 1800, "day_enrich": 1800, "news_collector": 25200,
    })
    disks: list[str] = field(default_factory=lambda: ["/", "/opt/ai_data"])
```

### E2b — `WatchdogService`

```python
# src/devforge/application/watchdog_service.py
"""Watchdog application service (legacy orchestrator.py main loop)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Optional

from devforge.core.config import WatchdogConfig
from devforge.domain.watchdog.monitoring.tracker import TrackerRegistry
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.incident_repository import IncidentRepository
from devforge.ports.notification import NotificationPort
from devforge.ports.recovery import RecoveryPort
from devforge.ports.state_persistence import StateStoragePort

log = logging.getLogger(__name__)

_EVENT_TYPE = {"svc": "down", "timer": "delay", "oneshot": "failed",
               "syssvc": "down", "llm": "down", "pipeline": "stuck",
               "system": "crit", "infra": "down"}


class WatchdogService:
    def __init__(self, config: WatchdogConfig, registry: TrackerRegistry,
                 check_coordinator: CheckCoordinator, recovery_coordinator: RecoveryCoordinator,
                 recovery_port: RecoveryPort, notification_ports: Sequence[NotificationPort],
                 incident_repo: IncidentRepository, state_storage: Optional[StateStoragePort] = None) -> None:
        self._config = config
        self._registry = registry
        self._checks = check_coordinator
        self._recovery = recovery_coordinator
        self._recovery_port = recovery_port
        self._notifiers = list(notification_ports)
        self._incidents = incident_repo
        self._state = state_storage
        self._mode = "day"
        self._last_heartbeat_ts = 0.0

    def _event_type(self, component: str) -> str:
        return _EVENT_TYPE.get(component.split(":", 1)[0], "down")

    async def run_cycle(self) -> dict[str, Any]:
        checks = await self._checks.run()
        failed = self._checks.failed(checks)

        # 1. healthy → resolve incidents (orchestrator.py:126)
        for c in checks:
            if c.is_healthy:
                await self._incidents.resolve_if_open(c.component)

        # 2. failed → incident → recovery → alert (orchestrator.py:128-143)
        for c in checks:
            if c.is_healthy:
                continue
            t = self._registry.get(c.component)
            inc_id = await self._incidents.record_detect(c.component, self._event_type(c.component), c.detail)
            action = self._recovery.plan(c.component, c.detail)
            if action is not None:
                if action.backoff_sec > 0:
                    await asyncio.sleep(action.backoff_sec)
                ok = await self._recovery_port.execute_recovery(action)
                self._recovery.record_result(c.component, ok)
                await self._incidents.record_action(inc_id, action.kind, ok)
                if ok:
                    for n in self._notifiers:
                        await n.send_recovery(c.component, f"{action.kind} ok")
            if t.is_degraded() and t.can_alert():
                for n in self._notifiers:
                    await n.send_alert(c.component, t.state.value, c.detail)

        self._persist()
        return {"checks": len(checks), "failed": len(failed),
                "timestamp": datetime.now(timezone.utc).isoformat()}

    def _persist(self) -> None:
        if self._state is not None:
            self._state.save(self._registry.all(), self._last_heartbeat_ts, self._mode)

    def load_state(self) -> None:
        if self._state is not None:
            payload = self._state.load()
            self._registry.restore({c["name"]: c for c in payload.get("components", []) if "name" in c})
            self._last_heartbeat_ts = float(payload.get("last_heartbeat_ts", 0.0))
            self._mode = payload.get("mode", self._mode)
```

**Tests**: `tests/unit/application/test_watchdog_service.py` (6) — fake ports: all healthy; single failure with recovery success; alert only when degraded+dedup; incident resolved on healthy; persistence save called; circuit-open skips recovery.

Commit: `feat(phase2): add WatchdogService + WatchdogConfig`

---

## Task E3: CLI (`adapters/driving/cli_cmds/watchdog.py` + composition root)

Driving adapter holds an injected factory (no application import):

```python
# src/devforge/adapters/driving/cli_cmds/watchdog.py
from __future__ import annotations
import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
import typer

app = typer.Typer(name="watchdog", help="Watchdog operations")
_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None


def init(factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    global _factory
    _factory = factory


def _service() -> Any:
    if _factory is None:
        raise RuntimeError("watchdog.init() not called from composition root")
    return asyncio.run(_factory())


@app.command("status")
def status() -> None:
    svc = _service()
    for t in svc._registry.all().values():   # noqa: SLF001 (diagnostic)
        typer.echo(f"{t.name}: {t.state.value} fails={t.fail_count} circuit={not t.can_retry()}")


@app.command("check")
def check() -> None:
    result = asyncio.run(_service().run_cycle())
    typer.echo(f"checks={result['checks']} failed={result['failed']}")


@app.command("resolve")
def resolve(incident_id: int, note: str) -> None:
    # resolve via incident repo
    ...
```

Composition root (`src/devforge/cli.py`):

```python
from devforge.adapters.driving.cli_cmds import watchdog as watchdog_cmds

def _watchdog_factory():
    from devforge.application.watchdog_service import create_watchdog_service
    from devforge.core.config import WatchdogConfig
    return create_watchdog_service(WatchdogConfig())

watchdog_cmds.init(_watchdog_factory)
app.add_typer(watchdog_cmds.app, name="watchdog")
```

`create_watchdog_service` builds: `TrackerRegistry`, health ports (C1–C4 + a `CompositeHealthChecker` for memory+disk), `SystemdRecoveryAdapter`, notifiers (Systemd + Slack if token), `PostgresIncidentRepository`, `JsonStateStorage`, and `load_state()`.

**Tests**: `tests/unit/adapters/driving/test_watchdog_cli.py` (4) — factory injection required; commands call service methods (mocked).

Commit: `feat(phase2): wire watchdog CLI via composition root`

---

# Verification Gates

### Gate 0: Phase 1 prerequisites
```bash
lint-imports                                   # 4 KEPT
pytest tests/unit tests/characterization -q    # green
devforge inference status
```

### Gate 1: Domain (after Phase A–B)
```bash
pytest tests/unit/ports tests/unit/domain/watchdog -v      # 52 passed
lint-imports                                               # 4 KEPT
```

### Gate 2: Adapters (after Phase C–D)
```bash
pytest tests/unit/adapters -v                              # 36 passed
pytest tests/characterization/test_*_parity.py -v          # parity (skips allowed offline)
```

### Gate 3: Integration (after Phase E)
```bash
pytest tests/unit/application tests/unit/adapters/driving -v   # 10 passed
devforge watchdog status
devforge watchdog check     # run inside the pod network (see Risk 1)
```

### Gate 4: Production switchover
1. `pytest tests/unit tests/characterization -q` green; `lint-imports` 4 KEPT; `mypy src/devforge` clean.
2. Backup legacy `scripts/lib/watchdog` + `watchdog_state.json`.
3. Deploy watchdog as a unit **on `devforge-net`** (not host — see Risk 1).
4. Shadow-run 24 h (no recovery) comparing new checks vs legacy; then enable recovery.
5. Rollback: stop new unit, restart `devforge-watchdog-legacy`.

# Commit Strategy (one commit per task)

```
Phase 0: refactor(watchdog): drop v1 Phase 2 implementation for legacy-parity v2
Phase A: A1 value objects · A2 backoff · A3 tracker+registry · A4 trend ETA · A5 persistence
Phase B: B1 recovery port+kinds · B2 recovery coordinator · B3 check coordinator
Phase C: C1 systemd · C2 llm · C3 system · C4 heartbeat  (+ parity)
Phase D: D1 slack · D2 sd_notify · D3 systemd recovery
Phase E: E1 incident repo · E2 service+config · E3 CLI wiring
Deploy : unit + docs + switchover + archive legacy
```

# Risk Mitigation

| # | Risk | Mitigation |
|---|------|-----------|
| 1 | **Host cannot reach Postgres** (no published 5432; only `devforge-net`) | Deploy watchdog on the pod network (container/quadlet) or reach DB via `podman exec`. The host `devforge watchdog check` cannot connect — verified in this environment. |
| 2 | `cascade` recovery is simplified | Keep `RecoveryPort` contract; implement full `recover_inference_cascade` escalation in Phase 3. Shadow-run before enabling. |
| 3 | State machine drift | `tests/characterization/test_watchdog_state_parity.py` (rewritten for v2 API) pins open@3, HALF_OPEN@120s, DOWN@5, reset@600s against legacy `state.py`. |
| 4 | State loss on switchover | A5 reads the legacy JSON shape; verify `watchdog_state.json` loads before cutover. |
| 5 | Notification/secret leakage | Never log tokens; mask context in incidents (`incidents.py:34-39`); Slack token from `secrets.env` only. |
| 6 | Performance | Target cycle < 20 s (legacy ~15 s); checks run per group; LLM probe timeout 60 s. |

# Appendix A: Legacy Function → Target

| Legacy | Target |
|--------|--------|
| `state.ComponentTracker` (`:79`) | `domain/monitoring/tracker.py` `ComponentTracker` |
| `state.WatchdogState.get` (`:229`) | `TrackerRegistry.get` |
| `state.TrendTracker` (`:28`) | `TrendTracker` (A4) |
| `state.save_state/load_state` (`:290`) | `JsonStateStorage` (A5) |
| `recovery.graduated_recover` (`:270`) | `RecoveryCoordinator.plan/record_result` (B2) |
| `recovery.recover_service/container/...` | `SystemdRecoveryAdapter.execute_recovery` (D3) |
| `checker.check_all_services/timers` | `SystemdService/TimerHealthChecker` (C1) |
| `checker.check_all_llm` | `LLMHealthChecker` (C2) |
| `checker.check_memory/disk` | `Memory/DiskHealthChecker` (C3) |
| `checker.check_heartbeats` | `HeartbeatHealthChecker` (C4) |
| `notifier.send_alert/send_recovery/sd_notify` | `SlackNotifier` / `SystemdNotifier` (D1/D2) |
| `incidents.record_detect/record_action/resolve_if_open` | `PostgresIncidentRepository` (E1) |
| `orchestrator.run_day_checks` main loop | `WatchdogService.run_cycle` (E2) |
| `codescanner.py`, `fixloop.py` | **Phase 3 (out of scope)** |

# Appendix B: Test Coverage Target

| Area | Unit | Parity | Target cov |
|------|------|--------|-----------|
| ports/types | 5 | 0 | >90% |
| monitoring (backoff/tracker/trend) | 6+14+7 | 1 | >90% |
| recovery (strategies/graduation) | 6+8 | 1 | >90% |
| orchestration (check) | 6 | 0 | >85% |
| health adapters | 20 | 4 | >80% |
| notification adapters | 6 | 0 | >75% |
| storage (state/incident) | 5+5 | 1 | >80% |
| application + cli | 6+4 | 0 | >80% |
| **Total** | **~98** | **~7** | **>85%** |

# Appendix C: Environment Variables

```bash
WATCHDOG_FAILURE_THRESHOLD=3
WATCHDOG_CIRCUIT_RESET_SEC=120
WATCHDOG_BACKOFF_RESET_SEC=600
WATCHDOG_CHECK_INTERVAL_SEC=60
WATCHDOG_CHECK_TIMEOUT_SEC=30
WATCHDOG_STATE_FILE=/opt/ai_data/scripts/watchdog_state.json
DEVFORGE_DATABASE_URL=postgresql+asyncpg://devforge:***@postgres:5432/devforge_app  # pod network
SLACK_BOT_TOKEN_KEY=***        # secrets.env
SLACK_CHANNEL=U0APJGD8CBW      # config.py:141
```

---

**End of guide (v2.1 — Phase A–E complete).**


