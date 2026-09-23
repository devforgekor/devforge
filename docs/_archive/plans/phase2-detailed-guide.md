# Phase 2 Detailed Implementation Guide — Watchdog Subsystem

> ⚠️ **SUPERSEDED** by `docs/plans/phase2-detailed-guide-v2.md` (v2.1 — legacy-parity, COMPLETE).
> This v1 guide is retained for reference only. **Do NOT implement from this file.**

## Overview

**Target**: `scripts/lib/watchdog` → `src/devforge/domain/watchdog` + adapters  
**Legacy Size**: 4,706 lines (11 modules)  
**Duration**: 4-5 days  
**Complexity**: HIGH (stateful, recovery logic, production-critical)

## Phase 1 Completion Verification (45min)

**CRITICAL**: Run this verification **BEFORE** starting Phase 2. Phase 2 assumes Phase 1 foundation is complete and working.

### Step 1: File Structure Check (5min)

Verify all Phase 1 files exist:

```bash
# Core modules (Week 1)
test -f src/devforge/core/database.py && echo "✅ database.py" || echo "❌ database.py MISSING"
test -f src/devforge/core/paths.py && echo "✅ paths.py" || echo "❌ paths.py MISSING"
test -f src/devforge/core/exceptions.py && echo "✅ exceptions.py" || echo "❌ exceptions.py MISSING"

# Ports (Week 1)
test -f src/devforge/ports/container.py && echo "✅ container.py" || echo "❌ container.py MISSING"

# Domain (Week 1)
test -f src/devforge/domain/model_management/registry.py && echo "✅ registry.py" || echo "❌ registry.py MISSING"

# Adapters (Week 1)
test -f src/devforge/adapters/driven/container/podman_adapter.py && echo "✅ podman_adapter.py" || echo "❌ podman_adapter.py MISSING"

# Application (Week 1)
test -f src/devforge/application/orchestrator.py && echo "✅ orchestrator.py" || echo "❌ orchestrator.py MISSING"

# CLI (Week 1)
test -f src/devforge/adapters/driving/cli_cmds/inference.py && echo "✅ inference.py" || echo "❌ inference.py MISSING"
```

**Expected**: All 8 files report ✅

**If any ❌**: Phase 1 incomplete. Review `/opt/projects/server/docs/plans/phase1-plan.md` and complete missing files.

---

### Step 2: Architecture Contracts (5min)

Verify import-linter contracts are enforced:

```bash
cd /opt/projects/server
lint-imports
```

**Expected Output**:
```
=============
Import Linter
=============

---------
Contracts
---------

layering: KEPT
hexagonal: KEPT
domain-subpackage-independence: KEPT
domain-agnostic-of-adapters: KEPT

Contracts: 4 kept, 0 broken.
```

**If any BROKEN**: Fix import violations before Phase 2. Phase 2 will add more dependencies and violations will compound.

---

### Step 3: Unit Tests (20min)

Run all Phase 1 unit tests:

```bash
cd /opt/projects/server

# Core module tests
pytest tests/unit/core/test_database.py -v
pytest tests/unit/core/test_paths.py -v
pytest tests/unit/core/test_exceptions.py -v
pytest tests/unit/core/test_config_watchdog.py -v

# Domain tests
pytest tests/unit/domain/model_management/ -v

# Adapter tests
pytest tests/unit/adapters/driven/container/ -v

# Summary
pytest tests/unit/ -q --tb=no
```

**Expected**:
- Individual test files: All PASSED
- Summary: **49 passed** (or higher if additional tests were added)

**If failures**: Investigate and fix. Common issues:
- Database connection (check `DEVFORGE_DB_URL`)
- Missing dependencies (`pip install -e .`)
- File permission issues

---

### Step 4: Characterization Tests (10min)

Run characterization tests to verify legacy behavior preservation:

```bash
cd /opt/projects/server

# Week 2 characterization tests
pytest tests/characterization/ -v
```

**Expected**: **19 passed** (5 test files × ~4 tests each)

**If failures**: Critical. Characterization test failures mean Phase 1 implementation diverged from legacy behavior. Must fix before Phase 2.

---

### Step 5: CLI Smoke Test (5min)

Verify CLI integration works:

```bash
# Test inference CLI
python3 cli.py inference status

# Expected output (no errors):
# Pod A: running (model: qwen3-reranker)
# Pod B: running (model: qwen3-7b-q8)
```

**If errors**:
- `ModuleNotFoundError`: Check PYTHONPATH and package installation
- `AttributeError`: Core modules not properly wired
- Database errors: Check postgres container status

---

### Step 6: Refactoring Status Check (5min)

Verify Phase 0 completion is recorded:

```bash
# Check refactoring status
python3 cli.py status --json | jq '.refactoring_status'

# Or directly read YAML
cat docs/architecture/REFACTORING_STATUS.yaml
```

**Expected**:
```yaml
phase0:
  status: COMPLETE
  completion_date: "2026-09-21"
  week1:
    status: COMPLETE
  week2:
    status: COMPLETE
```

**If phase0.status != "COMPLETE"**: Update status manually or investigate why Phase 0 was not finalized.

---

### Step 7: Documentation Check (5min)

Verify Phase 1 documentation exists:

```bash
# Check for Phase 1 work log
test -f docs/refactoring/phase0-work-log.md && echo "✅ Work log exists" || echo "❌ Work log missing"

# Check commit history
git log --oneline --since="2026-09-15" --until="2026-09-22" | grep -i "phase.*0\|core\|ports\|domain" | wc -l
# Expected: 15+ commits
```

---

## Verification Summary Checklist

Before proceeding to Phase 2, confirm:

- [ ] All 8 Phase 1 files exist (Step 1)
- [ ] import-linter: 4 contracts KEPT (Step 2)
- [ ] Unit tests: 49+ passed (Step 3)
- [ ] Characterization tests: 19 passed (Step 4)
- [ ] CLI smoke test: No errors (Step 5)
- [ ] Phase 0 status: COMPLETE (Step 6)
- [ ] Documentation: Work log exists (Step 7)

**If ALL checked**: ✅ **Phase 1 verification complete. Proceed to Phase 2.**

**If ANY unchecked**: ❌ **Do NOT start Phase 2. Fix Phase 1 issues first.**

---

## Troubleshooting Common Issues

### Issue 1: import-linter contract violations

```bash
# Find violating imports
lint-imports --verbose

# Common fix: Move import to Protocol/ABC
# Example: domain importing adapter directly
```

### Issue 2: Database tests failing

```bash
# Check postgres status
podman exec postgres psql -U devforge -d devforge_app -c "SELECT 1"

# Check connection string
echo $DEVFORGE_DB_URL
```

### Issue 3: Missing test dependencies

```bash
# Reinstall dev dependencies
pip install -e ".[dev]"

# Or manually install test tools
pip install pytest pytest-asyncio
```

---

### Phase 1 Foundation Status

✅ **Assumed Complete** (2026-09-21) — **Verify with steps above before Phase 2**:
- `core/{database,paths,exceptions,config}` — 4 modules
- `ports/container.py` — InferenceContainerManager protocol
- `domain/model_management/registry.py` — ModelRegistry
- `adapters/driven/container/podman_adapter.py` — PodmanContainerAdapter
- `application/orchestrator.py` — PipelineOrchestrator skeleton
- import-linter: 4 contracts KEPT
- Tests: 68 total (49 unit + 19 characterization)

## Architecture Vision

### Current (Legacy) Structure

```
scripts/lib/watchdog/
├── orchestrator.py     (1,122 lines) ← main entry, check/fix dispatch
├── checker.py          (784 lines)   ← all health check functions
├── state.py            (580 lines)   ← ComponentTracker, circuit breaker
├── recovery.py         (463 lines)   ← graduated recovery, restart logic
├── notifier.py         (376 lines)   ← Slack, systemd notify
├── codescanner.py      (323 lines)   ← AST-based code quality scan
├── fixloop.py          (291 lines)   ← experimental, sandbox verify
├── incidents.py        (288 lines)   ← DB incident tracking
├── messenger.py        (206 lines)   ← pulse/heartbeat DB ops
├── config.py           (195 lines)   ← constants, targets, thresholds
├── _globals.py         (18 lines)    ← shared mutable state
└── __init__.py         (60 lines)    ← re-exports
```

### Target (Hexagonal) Structure

```
src/devforge/
├── domain/watchdog/
│   ├── monitoring/
│   │   ├── tracker.py          ← ComponentTracker, TrendTracker (state.py)
│   │   ├── circuit_breaker.py  ← Circuit breaker logic
│   │   └── health_status.py    ← HealthCheck value objects
│   ├── recovery/
│   │   ├── strategies.py       ← Recovery strategy protocol + impls
│   │   ├── graduation.py       ← graduated_recover logic
│   │   └── actions.py          ← RecoveryAction domain events
│   ├── incidents/
│   │   ├── repository.py       ← Incident aggregate + repo protocol
│   │   └── tracker.py          ← Incident lifecycle
│   ├── scanning/
│   │   ├── code_quality.py     ← AST scanner (codescanner.py core)
│   │   └── rules.py            ← Scan rule definitions
│   └── orchestration/
│       ├── check_coordinator.py ← Check dispatch (orchestrator.py split)
│       └── fix_coordinator.py   ← Fix loop coordination
│
├── ports/
│   ├── health_check.py         ← HealthCheckPort protocol
│   ├── recovery.py             ← RecoveryPort protocol
│   ├── notification.py         ← NotificationPort protocol
│   └── incident_repository.py  ← IncidentRepositoryPort protocol
│
├── adapters/
│   ├── driven/
│   │   ├── health/
│   │   │   ├── systemd_health.py    ← check_all_services, timers
│   │   │   ├── llm_health.py        ← check_all_llm, probe
│   │   │   ├── system_health.py     ← check_memory, disk
│   │   │   └── pipeline_health.py   ← check_pipeline, heartbeats
│   │   ├── notification/
│   │   │   ├── slack_notifier.py    ← send_alert, send_recovery
│   │   │   └── systemd_notifier.py  ← sd_notify
│   │   └── storage/
│   │       ├── incident_pg.py       ← PostgreSQL incident repo
│   │       └── messenger_pg.py      ← pulse/heartbeat DB ops
│   └── driving/
│       └── cli_cmds/
│           └── watchdog.py          ← `cli.py watchdog` commands
│
└── application/
    └── watchdog_service.py          ← Watchdog application service
```

## Legacy Module Mapping

| Legacy Module | Target Location | Responsibility | Lines | Priority |
|---------------|-----------------|----------------|-------|----------|
| `state.py` | `domain/watchdog/monitoring/tracker.py` | State machine, circuit breaker | 580 | P0 |
| `orchestrator.py` (checks) | `domain/watchdog/orchestration/check_coordinator.py` | Check dispatch | ~400 | P0 |
| `orchestrator.py` (fixes) | `domain/watchdog/orchestration/fix_coordinator.py` | Fix dispatch | ~400 | P1 |
| `checker.py` | `adapters/driven/health/*.py` (4 files) | Health check implementations | 784 | P0 |
| `recovery.py` | `domain/watchdog/recovery/*.py` | Recovery strategies | 463 | P1 |
| `incidents.py` | `domain/watchdog/incidents/*.py` + `adapters/driven/storage/incident_pg.py` | Incident tracking | 288 | P2 |
| `notifier.py` | `adapters/driven/notification/*.py` | Slack + systemd | 376 | P2 |
| `messenger.py` | `adapters/driven/storage/messenger_pg.py` | Pulse/heartbeat DB | 206 | P2 |
| `codescanner.py` | `domain/watchdog/scanning/*.py` | AST code scanner | 323 | P3 |
| `config.py` | `core/config.py` (extend) | Constants | 195 | P0 |
| `fixloop.py` | (experimental, skip) | Sandbox verify | 291 | P4 |
| `_globals.py` | (eliminate) | Shared mutable state | 18 | P0 |

## Implementation Phases

### Phase A: Core Domain (Day 1-2, 1.5 days)

**Goal**: Extract stateless domain logic, eliminate shared mutable state

#### Task A1: State Machine + Circuit Breaker (4-5h)

**Input**: `state.py` (580 lines)

**Output**:
- `domain/watchdog/monitoring/tracker.py` (ComponentTracker, TrendTracker)
- `domain/watchdog/monitoring/circuit_breaker.py` (circuit breaker logic)
- `domain/watchdog/monitoring/health_status.py` (value objects)

**Steps**:

1. **Extract value objects** (30min):
```python
# domain/watchdog/monitoring/health_status.py
from dataclasses import dataclass
from enum import Enum

class ComponentState(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    RECOVERING = "recovering"

@dataclass(frozen=True)
class HealthCheckResult:
    component: str
    ok: bool
    detail: str
    timestamp: datetime

@dataclass(frozen=True)
class ComponentStatus:
    name: str
    state: ComponentState
    fail_count: int
    consecutive_failures: int
    circuit_open: bool
    last_success: datetime | None
    last_failure: datetime | None
```

2. **Port ComponentTracker** (2h):
```python
# domain/watchdog/monitoring/tracker.py
from devforge.domain.watchdog.monitoring.health_status import ComponentState, ComponentStatus

class TrendTracker:
    """Track failure trends for circuit breaker decisions."""
    
    def __init__(self, window_size: int = 10):
        self._window_size = window_size
        self._history: dict[str, list[bool]] = {}
    
    def record(self, component: str, success: bool) -> None:
        if component not in self._history:
            self._history[component] = []
        self._history[component].append(success)
        if len(self._history[component]) > self._window_size:
            self._history[component].pop(0)
    
    def failure_rate(self, component: str) -> float:
        if component not in self._history or not self._history[component]:
            return 0.0
        failures = sum(1 for ok in self._history[component] if not ok)
        return failures / len(self._history[component])

class ComponentTracker:
    """Track component health status and circuit breaker state.
    
    Pure domain logic — no I/O, no side effects.
    """
    
    def __init__(self, 
                 failure_threshold: int = 3,
                 success_threshold: int = 2):
        self._threshold_fail = failure_threshold
        self._threshold_success = success_threshold
        self._states: dict[str, ComponentStatus] = {}
        self._trend = TrendTracker()
    
    def record_check(self, result: HealthCheckResult) -> ComponentStatus:
        """Record health check result and update component status."""
        current = self._states.get(result.component)
        
        if current is None:
            # First check
            return self._initialize_component(result)
        
        return self._transition(current, result)
    
    def _initialize_component(self, result: HealthCheckResult) -> ComponentStatus:
        status = ComponentStatus(
            name=result.component,
            state=ComponentState.HEALTHY if result.ok else ComponentState.DEGRADED,
            fail_count=0 if result.ok else 1,
            consecutive_failures=0 if result.ok else 1,
            circuit_open=False,
            last_success=result.timestamp if result.ok else None,
            last_failure=None if result.ok else result.timestamp,
        )
        self._states[result.component] = status
        self._trend.record(result.component, result.ok)
        return status
    
    def _transition(self, current: ComponentStatus, result: HealthCheckResult) -> ComponentStatus:
        """State machine transition logic."""
        self._trend.record(result.component, result.ok)
        
        if result.ok:
            return self._handle_success(current, result)
        else:
            return self._handle_failure(current, result)
    
    def _handle_success(self, current: ComponentStatus, result: HealthCheckResult) -> ComponentStatus:
        """Handle successful health check."""
        new_state = current.state
        circuit_open = current.circuit_open
        
        if current.state == ComponentState.RECOVERING:
            # Check if we can close circuit
            if current.consecutive_failures == 0:
                consecutive_success = self._count_recent_success(result.component)
                if consecutive_success >= self._threshold_success:
                    new_state = ComponentState.HEALTHY
                    circuit_open = False
        
        updated = ComponentStatus(
            name=current.name,
            state=new_state,
            fail_count=current.fail_count,
            consecutive_failures=0,  # Reset
            circuit_open=circuit_open,
            last_success=result.timestamp,
            last_failure=current.last_failure,
        )
        self._states[result.component] = updated
        return updated
    
    def _handle_failure(self, current: ComponentStatus, result: HealthCheckResult) -> ComponentStatus:
        """Handle failed health check."""
        new_fail_count = current.fail_count + 1
        new_consecutive = current.consecutive_failures + 1
        new_state = current.state
        circuit_open = current.circuit_open
        
        # State transitions based on failure count
        if new_consecutive >= self._threshold_fail:
            if current.state == ComponentState.HEALTHY:
                new_state = ComponentState.DEGRADED
            elif current.state == ComponentState.DEGRADED:
                new_state = ComponentState.CRITICAL
                circuit_open = True  # Open circuit
        
        updated = ComponentStatus(
            name=current.name,
            state=new_state,
            fail_count=new_fail_count,
            consecutive_failures=new_consecutive,
            circuit_open=circuit_open,
            last_success=current.last_success,
            last_failure=result.timestamp,
        )
        self._states[result.component] = updated
        return updated
    
    def _count_recent_success(self, component: str) -> int:
        """Count consecutive recent successes."""
        if component not in self._trend._history:
            return 0
        history = self._trend._history[component]
        count = 0
        for ok in reversed(history):
            if ok:
                count += 1
            else:
                break
        return count
    
    def get_status(self, component: str) -> ComponentStatus | None:
        return self._states.get(component)
    
    def all_statuses(self) -> list[ComponentStatus]:
        return list(self._states.values())
```

3. **Extract circuit breaker** (1h):
```python
# domain/watchdog/monitoring/circuit_breaker.py
from devforge.domain.watchdog.monitoring.health_status import ComponentStatus

class CircuitBreaker:
    """Circuit breaker pattern for component protection.
    
    Prevents cascading failures by blocking operations when
    a component is in CRITICAL state.
    """
    
    def __init__(self, tracker: ComponentTracker):
        self._tracker = tracker
    
    def is_open(self, component: str) -> bool:
        """Check if circuit is open for component."""
        status = self._tracker.get_status(component)
        return status is not None and status.circuit_open
    
    def should_allow_check(self, component: str) -> bool:
        """Determine if health check should proceed."""
        return not self.is_open(component)
    
    def should_allow_recovery(self, component: str) -> bool:
        """Determine if recovery attempt should proceed."""
        status = self._tracker.get_status(component)
        if status is None:
            return True
        
        # Allow recovery if not in circuit-open state
        # or if enough time has passed (half-open state)
        return not status.circuit_open or self._should_attempt_reset(status)
    
    def _should_attempt_reset(self, status: ComponentStatus) -> bool:
        """Check if circuit should attempt reset (half-open)."""
        if not status.circuit_open:
            return False
        
        if status.last_failure is None:
            return True
        
        # Attempt reset after 5 minutes
        elapsed = datetime.now(UTC) - status.last_failure
        return elapsed.total_seconds() > 300
```

4. **Write tests** (1.5h):
```python
# tests/unit/domain/watchdog/monitoring/test_tracker.py
import pytest
from datetime import datetime, UTC
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker, TrendTracker
from devforge.domain.watchdog.monitoring.health_status import (
    HealthCheckResult, ComponentState
)

class TestTrendTracker:
    def test_empty_failure_rate(self):
        tracker = TrendTracker(window_size=5)
        assert tracker.failure_rate("unknown") == 0.0
    
    def test_records_success(self):
        tracker = TrendTracker(window_size=5)
        tracker.record("svc", True)
        assert tracker.failure_rate("svc") == 0.0
    
    def test_records_failure(self):
        tracker = TrendTracker(window_size=5)
        tracker.record("svc", False)
        assert tracker.failure_rate("svc") == 1.0
    
    def test_sliding_window(self):
        tracker = TrendTracker(window_size=3)
        tracker.record("svc", False)
        tracker.record("svc", False)
        tracker.record("svc", True)
        assert tracker.failure_rate("svc") == pytest.approx(2/3)
        
        # Window slides
        tracker.record("svc", True)
        assert tracker.failure_rate("svc") == pytest.approx(1/3)

class TestComponentTracker:
    def test_first_check_success(self):
        tracker = ComponentTracker(failure_threshold=3, success_threshold=2)
        result = HealthCheckResult(
            component="svc1",
            ok=True,
            detail="OK",
            timestamp=datetime.now(UTC)
        )
        status = tracker.record_check(result)
        
        assert status.state == ComponentState.HEALTHY
        assert status.consecutive_failures == 0
        assert not status.circuit_open
    
    def test_first_check_failure(self):
        tracker = ComponentTracker(failure_threshold=3, success_threshold=2)
        result = HealthCheckResult(
            component="svc1",
            ok=False,
            detail="Connection refused",
            timestamp=datetime.now(UTC)
        )
        status = tracker.record_check(result)
        
        assert status.state == ComponentState.DEGRADED
        assert status.consecutive_failures == 1
        assert not status.circuit_open
    
    def test_degraded_after_threshold(self):
        tracker = ComponentTracker(failure_threshold=3, success_threshold=2)
        
        for i in range(3):
            result = HealthCheckResult(
                component="svc1",
                ok=False,
                detail=f"Fail {i}",
                timestamp=datetime.now(UTC)
            )
            status = tracker.record_check(result)
        
        assert status.state == ComponentState.DEGRADED
        assert status.consecutive_failures == 3
    
    def test_critical_opens_circuit(self):
        tracker = ComponentTracker(failure_threshold=3, success_threshold=2)
        
        # First: degrade
        for i in range(3):
            tracker.record_check(HealthCheckResult(
                component="svc1", ok=False, detail="", timestamp=datetime.now(UTC)
            ))
        
        # Continue failing: critical
        for i in range(3):
            status = tracker.record_check(HealthCheckResult(
                component="svc1", ok=False, detail="", timestamp=datetime.now(UTC)
            ))
        
        assert status.state == ComponentState.CRITICAL
        assert status.circuit_open
    
    def test_recovery_closes_circuit(self):
        tracker = ComponentTracker(failure_threshold=3, success_threshold=2)
        
        # Degrade + circuit open
        for i in range(6):
            tracker.record_check(HealthCheckResult(
                component="svc1", ok=False, detail="", timestamp=datetime.now(UTC)
            ))
        
        # Start recovering
        for i in range(2):
            status = tracker.record_check(HealthCheckResult(
                component="svc1", ok=True, detail="OK", timestamp=datetime.now(UTC)
            ))
        
        # Should transition to HEALTHY and close circuit
        assert status.state == ComponentState.HEALTHY
        assert not status.circuit_open
```

**Verification**:
```bash
pytest tests/unit/domain/watchdog/monitoring/ -v
# Expected: 12+ tests PASSED
```

---

#### Task A2: Recovery Strategies (3-4h)

**Input**: `recovery.py` (463 lines)

**Output**:
- `domain/watchdog/recovery/strategies.py` (protocols + implementations)
- `domain/watchdog/recovery/graduation.py` (graduated recovery logic)
- `domain/watchdog/recovery/actions.py` (domain events)

**Steps**:

1. **Define protocols** (30min):
```python
# domain/watchdog/recovery/strategies.py
from typing import Protocol
from dataclasses import dataclass

@dataclass(frozen=True)
class RecoveryAction:
    """Domain event: recovery action to be executed."""
    component: str
    action_type: str  # "restart", "reload", "reset", "escalate"
    detail: str
    severity: int  # 0=soft, 1=medium, 2=hard

class RecoveryStrategy(Protocol):
    """Protocol for component recovery strategies."""
    
    def can_handle(self, component: str, status: ComponentStatus) -> bool:
        """Check if this strategy applies."""
        ...
    
    def recover(self, component: str, status: ComponentStatus) -> RecoveryAction:
        """Generate recovery action."""
        ...

class GraduatedRecoveryStrategy:
    """Graduated recovery: escalate action based on failure count."""
    
    def __init__(self):
        self._escalation_levels = [
            (1, "soft", "Soft restart"),
            (3, "medium", "Service restart"),
            (5, "hard", "Hard reset + dependency restart"),
        ]
    
    def can_handle(self, component: str, status: ComponentStatus) -> bool:
        return status.state in (ComponentState.DEGRADED, ComponentState.CRITICAL)
    
    def recover(self, component: str, status: ComponentStatus) -> RecoveryAction:
        severity = self._determine_severity(status.fail_count)
        action_type = self._map_action(severity)
        
        return RecoveryAction(
            component=component,
            action_type=action_type,
            detail=f"{action_type.title()} recovery (fail_count={status.fail_count})",
            severity=severity
        )
    
    def _determine_severity(self, fail_count: int) -> int:
        for threshold, level, _ in reversed(self._escalation_levels):
            if fail_count >= threshold:
                return ["soft", "medium", "hard"].index(level)
        return 0
    
    def _map_action(self, severity: int) -> str:
        mapping = {0: "restart", 1: "reload", 2: "reset"}
        return mapping.get(severity, "escalate")
```

2. **Port graduated_recover** (2h):
```python
# domain/watchdog/recovery/graduation.py
from devforge.domain.watchdog.recovery.strategies import RecoveryAction, GraduatedRecoveryStrategy
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker

class RecoveryCoordinator:
    """Coordinate recovery actions based on component health."""
    
    def __init__(self, tracker: ComponentTracker):
        self._tracker = tracker
        self._strategies: list[RecoveryStrategy] = [
            GraduatedRecoveryStrategy(),
        ]
    
    def plan_recovery(self, component: str) -> RecoveryAction | None:
        """Plan recovery action for component."""
        status = self._tracker.get_status(component)
        if status is None:
            return None
        
        for strategy in self._strategies:
            if strategy.can_handle(component, status):
                return strategy.recover(component, status)
        
        return None
    
    def should_escalate(self, component: str) -> bool:
        """Check if component needs escalation (human intervention)."""
        status = self._tracker.get_status(component)
        if status is None:
            return False
        
        # Escalate if critical + many failures
        return status.state == ComponentState.CRITICAL and status.fail_count > 10
```

3. **Write tests** (1.5h):
```python
# tests/unit/domain/watchdog/recovery/test_strategies.py
import pytest
from devforge.domain.watchdog.recovery.strategies import GraduatedRecoveryStrategy, RecoveryAction
from devforge.domain.watchdog.monitoring.health_status import ComponentStatus, ComponentState
from datetime import datetime, UTC

class TestGraduatedRecoveryStrategy:
    def test_soft_recovery_at_first_failure(self):
        strategy = GraduatedRecoveryStrategy()
        status = ComponentStatus(
            name="svc1",
            state=ComponentState.DEGRADED,
            fail_count=1,
            consecutive_failures=1,
            circuit_open=False,
            last_success=None,
            last_failure=datetime.now(UTC)
        )
        
        action = strategy.recover("svc1", status)
        assert action.severity == 0
        assert action.action_type == "restart"
    
    def test_medium_recovery_at_threshold(self):
        strategy = GraduatedRecoveryStrategy()
        status = ComponentStatus(
            name="svc1",
            state=ComponentState.DEGRADED,
            fail_count=3,
            consecutive_failures=3,
            circuit_open=False,
            last_success=None,
            last_failure=datetime.now(UTC)
        )
        
        action = strategy.recover("svc1", status)
        assert action.severity == 1
        assert action.action_type == "reload"
    
    def test_hard_recovery_at_high_threshold(self):
        strategy = GraduatedRecoveryStrategy()
        status = ComponentStatus(
            name="svc1",
            state=ComponentState.CRITICAL,
            fail_count=5,
            consecutive_failures=5,
            circuit_open=True,
            last_success=None,
            last_failure=datetime.now(UTC)
        )
        
        action = strategy.recover("svc1", status)
        assert action.severity == 2
        assert action.action_type == "reset"
```

**Verification**:
```bash
pytest tests/unit/domain/watchdog/recovery/ -v
# Expected: 8+ tests PASSED
```

---

#### Task A3: Check/Fix Coordinators (3-4h)

**Input**: `orchestrator.py` (1,122 lines, split into check/fix)

**Output**:
- `domain/watchdog/orchestration/check_coordinator.py` (~400 lines)
- `domain/watchdog/orchestration/fix_coordinator.py` (~400 lines)

**Steps**:

1. **Extract check coordination** (2h):
```python
# domain/watchdog/orchestration/check_coordinator.py
from dataclasses import dataclass
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult
from devforge.ports.health_check import HealthCheckPort

@dataclass
class CheckPlan:
    """Plan for executing health checks."""
    components: list[str]
    parallel: bool = True
    timeout_per_check: int = 30

class CheckCoordinator:
    """Coordinate health check execution across components.
    
    Pure orchestration logic — delegates actual checks to adapters via ports.
    """
    
    def __init__(self, 
                 tracker: ComponentTracker,
                 health_ports: dict[str, HealthCheckPort]):
        self._tracker = tracker
        self._health_ports = health_ports
    
    def execute_checks(self, plan: CheckPlan) -> list[HealthCheckResult]:
        """Execute health checks according to plan."""
        results = []
        
        for component in plan.components:
            port = self._health_ports.get(component)
            if port is None:
                results.append(HealthCheckResult(
                    component=component,
                    ok=False,
                    detail="No health check port registered",
                    timestamp=datetime.now(UTC)
                ))
                continue
            
            result = port.check_health()
            # Update tracker
            self._tracker.record_check(result)
            results.append(result)
        
        return results
    
    def should_skip_check(self, component: str) -> bool:
        """Determine if check should be skipped (circuit open)."""
        status = self._tracker.get_status(component)
        if status is None:
            return False
        return status.circuit_open
```

2. **Extract fix coordination** (2h):
```python
# domain/watchdog/orchestration/fix_coordinator.py
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.domain.watchdog.recovery.strategies import RecoveryAction
from devforge.ports.recovery import RecoveryPort

class FixCoordinator:
    """Coordinate fix/recovery actions across components."""
    
    def __init__(self,
                 recovery_coordinator: RecoveryCoordinator,
                 recovery_ports: dict[str, RecoveryPort]):
        self._recovery = recovery_coordinator
        self._recovery_ports = recovery_ports
    
    def execute_fixes(self, failed_components: list[str]) -> dict[str, bool]:
        """Execute recovery actions for failed components."""
        results = {}
        
        for component in failed_components:
            action = self._recovery.plan_recovery(component)
            if action is None:
                results[component] = False
                continue
            
            port = self._recovery_ports.get(component)
            if port is None:
                results[component] = False
                continue
            
            success = port.execute_recovery(action)
            results[component] = success
        
        return results
    
    def should_escalate(self, component: str) -> bool:
        """Check if component needs human intervention."""
        return self._recovery.should_escalate(component)
```

**Verification**:
```bash
pytest tests/unit/domain/watchdog/orchestration/ -v
# Expected: 6+ tests PASSED
```

---

#### Task A4: Config Migration (1h)

**Input**: `config.py` (195 lines)

**Output**: Extend `core/config.py`

**Steps**:

1. **Add watchdog config** (45min):
```python
# core/config.py (extend existing)

@dataclass(frozen=True)
class WatchdogConfig:
    """Watchdog subsystem configuration."""
    
    # Circuit breaker
    failure_threshold: int = 3
    success_threshold: int = 2
    circuit_reset_timeout_sec: int = 300
    
    # Recovery
    recovery_escalation_levels: list[tuple[int, str]] = field(default_factory=lambda: [
        (1, "soft"),
        (3, "medium"),
        (5, "hard"),
    ])
    
    # Health checks
    check_interval_sec: int = 60
    check_timeout_sec: int = 30
    
    # Targets
    critical_services: list[str] = field(default_factory=lambda: [
        "devforge-fastapi",
        "devforge-mcp",
        "postgres",
    ])
    
    llm_targets: dict[str, int] = field(default_factory=lambda: {
        "pod-a": 11434,
        "pod-b": 11435,
    })
    
    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        """Load from environment variables."""
        return cls(
            failure_threshold=int(os.getenv("WATCHDOG_FAILURE_THRESHOLD", "3")),
            success_threshold=int(os.getenv("WATCHDOG_SUCCESS_THRESHOLD", "2")),
            circuit_reset_timeout_sec=int(os.getenv("WATCHDOG_CIRCUIT_RESET_SEC", "300")),
            check_interval_sec=int(os.getenv("WATCHDOG_CHECK_INTERVAL_SEC", "60")),
            check_timeout_sec=int(os.getenv("WATCHDOG_CHECK_TIMEOUT_SEC", "30")),
        )

# Extend DevForgeConfig
@dataclass(frozen=True)
class DevForgeConfig:
    # ... existing fields ...
    watchdog: WatchdogConfig = field(default_factory=WatchdogConfig.from_env)
```

2. **Write tests** (15min):
```python
# tests/unit/core/test_config_watchdog.py
import pytest
import os
from devforge.core.config import WatchdogConfig

class TestWatchdogConfig:
    def test_defaults(self):
        config = WatchdogConfig()
        assert config.failure_threshold == 3
        assert config.success_threshold == 2
        assert config.circuit_reset_timeout_sec == 300
    
    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("WATCHDOG_FAILURE_THRESHOLD", "5")
        monkeypatch.setenv("WATCHDOG_CHECK_INTERVAL_SEC", "120")
        
        config = WatchdogConfig.from_env()
        assert config.failure_threshold == 5
        assert config.check_interval_sec == 120
```

---

### Phase B: Health Check Adapters (Day 2-3, 1 day)

**Goal**: Port 784 lines of health check implementations from `checker.py`

#### Task B1: Systemd Health Adapter (2-3h)

**Input**: `checker.py` (check_all_services, check_all_timers functions)

**Output**: `adapters/driven/health/systemd_health.py`

**Steps**:

1. **Define port** (15min):
```python
# ports/health_check.py
from typing import Protocol
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult

class HealthCheckPort(Protocol):
    """Port for health check implementations."""
    
    def check_health(self) -> HealthCheckResult:
        """Execute health check and return result."""
        ...
```

2. **Implement systemd adapter** (2h):
```python
# adapters/driven/health/systemd_health.py
import subprocess
from datetime import datetime, UTC
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult
from devforge.ports.health_check import HealthCheckPort

class SystemdServiceHealthCheck(HealthCheckPort):
    """Health check for systemd user services."""
    
    def __init__(self, service_names: list[str]):
        self._services = service_names
    
    def check_health(self) -> HealthCheckResult:
        """Check if all services are active."""
        failed = []
        
        for service in self._services:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", service],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                failed.append(service)
        
        if failed:
            return HealthCheckResult(
                component="systemd-services",
                ok=False,
                detail=f"Failed services: {', '.join(failed)}",
                timestamp=datetime.now(UTC)
            )
        
        return HealthCheckResult(
            component="systemd-services",
            ok=True,
            detail=f"All {len(self._services)} services active",
            timestamp=datetime.now(UTC)
        )

class SystemdTimerHealthCheck(HealthCheckPort):
    """Health check for systemd timers."""
    
    def __init__(self, timer_names: list[str]):
        self._timers = timer_names
    
    def check_health(self) -> HealthCheckResult:
        """Check if all timers are active."""
        failed = []
        
        for timer in self._timers:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", timer],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode != 0:
                failed.append(timer)
        
        if failed:
            return HealthCheckResult(
                component="systemd-timers",
                ok=False,
                detail=f"Failed timers: {', '.join(failed)}",
                timestamp=datetime.now(UTC)
            )
        
        return HealthCheckResult(
            component="systemd-timers",
            ok=True,
            detail=f"All {len(self._timers)} timers active",
            timestamp=datetime.now(UTC)
        )
```

3. **Write parity tests** (1h):
```python
# tests/unit/adapters/driven/health/test_systemd_health.py
import pytest
from unittest.mock import patch, MagicMock
from devforge.adapters.driven.health.systemd_health import SystemdServiceHealthCheck

class TestSystemdServiceHealthCheck:
    @patch("subprocess.run")
    def test_all_services_active(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        
        checker = SystemdServiceHealthCheck(["svc1", "svc2"])
        result = checker.check_health()
        
        assert result.ok
        assert "All 2 services active" in result.detail
    
    @patch("subprocess.run")
    def test_some_services_failed(self, mock_run):
        # First call succeeds, second fails
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=3)
        ]
        
        checker = SystemdServiceHealthCheck(["svc1", "svc2"])
        result = checker.check_health()
        
        assert not result.ok
        assert "svc2" in result.detail
```

**Legacy Parity Test**:
```python
# tests/characterization/test_watchdog_systemd_parity.py
"""Verify new systemd health adapter matches legacy checker.py behavior."""
import subprocess
from devforge.adapters.driven.health.systemd_health import SystemdServiceHealthCheck

def test_systemd_service_parity():
    """New adapter should detect same failures as legacy."""
    # Get actual service status via legacy method
    legacy_result = subprocess.run(
        ["systemctl", "--user", "is-active", "devforge-fastapi"],
        capture_output=True
    )
    legacy_ok = (legacy_result.returncode == 0)
    
    # New adapter
    checker = SystemdServiceHealthCheck(["devforge-fastapi"])
    new_result = checker.check_health()
    
    assert new_result.ok == legacy_ok
```

---

#### Task B2: LLM Health Adapter (2-3h)

**Input**: `checker.py` (check_all_llm, llm_probe functions)

**Output**: `adapters/driven/health/llm_health.py`

**Steps**:

1. **Implement LLM probe** (2h):
```python
# adapters/driven/health/llm_health.py
import httpx
from datetime import datetime, UTC
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult
from devforge.ports.health_check import HealthCheckPort

class LLMHealthCheck(HealthCheckPort):
    """Health check for LLM inference pods."""
    
    def __init__(self, targets: dict[str, int], timeout: int = 10):
        self._targets = targets  # {"pod-a": 11434, "pod-b": 11435}
        self._timeout = timeout
    
    def check_health(self) -> HealthCheckResult:
        """Check if all LLM pods are responding."""
        failed = []
        
        for pod_name, port in self._targets.items():
            if not self._probe_pod(pod_name, port):
                failed.append(pod_name)
        
        if failed:
            return HealthCheckResult(
                component="llm-pods",
                ok=False,
                detail=f"Failed pods: {', '.join(failed)}",
                timestamp=datetime.now(UTC)
            )
        
        return HealthCheckResult(
            component="llm-pods",
            ok=True,
            detail=f"All {len(self._targets)} pods responding",
            timestamp=datetime.now(UTC)
        )
    
    def _probe_pod(self, pod_name: str, port: int) -> bool:
        """Probe single LLM pod via /api/tags."""
        try:
            response = httpx.get(
                f"http://localhost:{port}/api/tags",
                timeout=self._timeout
            )
            return response.status_code == 200
        except Exception:
            return False
```

2. **Write tests** (1h):
```python
# tests/unit/adapters/driven/health/test_llm_health.py
import pytest
from unittest.mock import patch, MagicMock
from devforge.adapters.driven.health.llm_health import LLMHealthCheck

class TestLLMHealthCheck:
    @patch("httpx.get")
    def test_all_pods_healthy(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200)
        
        checker = LLMHealthCheck({"pod-a": 11434, "pod-b": 11435})
        result = checker.check_health()
        
        assert result.ok
        assert "All 2 pods responding" in result.detail
    
    @patch("httpx.get")
    def test_one_pod_down(self, mock_get):
        # First call succeeds, second fails
        mock_get.side_effect = [
            MagicMock(status_code=200),
            Exception("Connection refused")
        ]
        
        checker = LLMHealthCheck({"pod-a": 11434, "pod-b": 11435})
        result = checker.check_health()
        
        assert not result.ok
        assert "pod-b" in result.detail
```

---

#### Task B3: System Health Adapter (1-2h)

**Input**: `checker.py` (check_memory, check_disk functions)

**Output**: `adapters/driven/health/system_health.py`

**Steps**:

1. **Implement system checks** (1h):
```python
# adapters/driven/health/system_health.py
import psutil
from datetime import datetime, UTC
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult
from devforge.ports.health_check import HealthCheckPort

class MemoryHealthCheck(HealthCheckPort):
    """Health check for system memory."""
    
    def __init__(self, threshold_percent: float = 90.0):
        self._threshold = threshold_percent
    
    def check_health(self) -> HealthCheckResult:
        """Check if memory usage is below threshold."""
        mem = psutil.virtual_memory()
        usage_pct = mem.percent
        
        if usage_pct >= self._threshold:
            return HealthCheckResult(
                component="memory",
                ok=False,
                detail=f"Memory usage {usage_pct:.1f}% >= {self._threshold}%",
                timestamp=datetime.now(UTC)
            )
        
        return HealthCheckResult(
            component="memory",
            ok=True,
            detail=f"Memory usage {usage_pct:.1f}%",
            timestamp=datetime.now(UTC)
        )

class DiskHealthCheck(HealthCheckPort):
    """Health check for disk space."""
    
    def __init__(self, path: str, threshold_percent: float = 90.0):
        self._path = path
        self._threshold = threshold_percent
    
    def check_health(self) -> HealthCheckResult:
        """Check if disk usage is below threshold."""
        disk = psutil.disk_usage(self._path)
        usage_pct = disk.percent
        
        if usage_pct >= self._threshold:
            return HealthCheckResult(
                component=f"disk-{self._path}",
                ok=False,
                detail=f"Disk usage {usage_pct:.1f}% >= {self._threshold}%",
                timestamp=datetime.now(UTC)
            )
        
        return HealthCheckResult(
            component=f"disk-{self._path}",
            ok=True,
            detail=f"Disk usage {usage_pct:.1f}%",
            timestamp=datetime.now(UTC)
        )
```

2. **Write tests** (1h):
```python
# tests/unit/adapters/driven/health/test_system_health.py
import pytest
from unittest.mock import patch, MagicMock
from devforge.adapters.driven.health.system_health import MemoryHealthCheck

class TestMemoryHealthCheck:
    @patch("psutil.virtual_memory")
    def test_memory_ok(self, mock_vm):
        mock_vm.return_value = MagicMock(percent=75.0)
        
        checker = MemoryHealthCheck(threshold_percent=90.0)
        result = checker.check_health()
        
        assert result.ok
        assert "75.0%" in result.detail
    
    @patch("psutil.virtual_memory")
    def test_memory_critical(self, mock_vm):
        mock_vm.return_value = MagicMock(percent=95.0)
        
        checker = MemoryHealthCheck(threshold_percent=90.0)
        result = checker.check_health()
        
        assert not result.ok
        assert "95.0%" in result.detail
```

---

#### Task B4: Pipeline Health Adapter (1-2h)

**Input**: `checker.py` (check_pipeline, heartbeat functions)

**Output**: `adapters/driven/health/pipeline_health.py`

**Steps**:

1. **Implement pipeline checks** (1h):
```python
# adapters/driven/health/pipeline_health.py
from datetime import datetime, UTC, timedelta
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult
from devforge.ports.health_check import HealthCheckPort
from devforge.core.database import DatabaseGateway

class PipelineHeartbeatCheck(HealthCheckPort):
    """Health check for pipeline heartbeats."""
    
    def __init__(self, db: DatabaseGateway, stale_threshold_sec: int = 300):
        self._db = db
        self._threshold = stale_threshold_sec
    
    async def check_health(self) -> HealthCheckResult:
        """Check if pipeline heartbeats are recent."""
        query = """
            SELECT name, last_heartbeat_at
            FROM pulse_tracking
            WHERE resolved_at IS NULL
            ORDER BY last_heartbeat_at DESC
        """
        
        async with self._db.session() as session:
            result = await session.execute(query)
            pulses = result.fetchall()
        
        stale = []
        now = datetime.now(UTC)
        
        for name, last_heartbeat in pulses:
            if last_heartbeat is None:
                stale.append(name)
                continue
            
            elapsed = (now - last_heartbeat).total_seconds()
            if elapsed > self._threshold:
                stale.append(f"{name} ({int(elapsed)}s)")
        
        if stale:
            return HealthCheckResult(
                component="pipeline-heartbeats",
                ok=False,
                detail=f"Stale heartbeats: {', '.join(stale)}",
                timestamp=now
            )
        
        return HealthCheckResult(
            component="pipeline-heartbeats",
            ok=True,
            detail=f"{len(pulses)} active pipelines",
            timestamp=now
        )
```

2. **Write tests** (1h):
```python
# tests/unit/adapters/driven/health/test_pipeline_health.py
import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import AsyncMock, MagicMock
from devforge.adapters.driven.health.pipeline_health import PipelineHeartbeatCheck

@pytest.mark.asyncio
async def test_all_heartbeats_fresh():
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    
    now = datetime.now(UTC)
    mock_result.fetchall.return_value = [
        ("pipeline1", now - timedelta(seconds=60)),
        ("pipeline2", now - timedelta(seconds=120)),
    ]
    mock_session.execute.return_value = mock_result
    mock_db.session.return_value.__aenter__.return_value = mock_session
    
    checker = PipelineHeartbeatCheck(mock_db, stale_threshold_sec=300)
    result = await checker.check_health()
    
    assert result.ok
    assert "2 active pipelines" in result.detail

@pytest.mark.asyncio
async def test_stale_heartbeat_detected():
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    
    now = datetime.now(UTC)
    mock_result.fetchall.return_value = [
        ("pipeline1", now - timedelta(seconds=60)),
        ("pipeline2", now - timedelta(seconds=400)),  # Stale
    ]
    mock_session.execute.return_value = mock_result
    mock_db.session.return_value.__aenter__.return_value = mock_session
    
    checker = PipelineHeartbeatCheck(mock_db, stale_threshold_sec=300)
    result = await checker.check_health()
    
    assert not result.ok
    assert "pipeline2" in result.detail
```

---

### Phase C: Recovery & Notification Adapters (Day 3-4, 1 day)

**Goal**: Port recovery execution and notification logic

#### Task C1: Recovery Port + Systemd Recovery Adapter (2-3h)

**Input**: `recovery.py` restart/reload logic

**Output**: 
- `ports/recovery.py`
- `adapters/driven/recovery/systemd_recovery.py`

**Steps**:

1. **Define port** (15min):
```python
# ports/recovery.py
from typing import Protocol
from devforge.domain.watchdog.recovery.strategies import RecoveryAction

class RecoveryPort(Protocol):
    """Port for recovery action execution."""
    
    def execute_recovery(self, action: RecoveryAction) -> bool:
        """Execute recovery action and return success status."""
        ...
```

2. **Implement systemd recovery** (2h):
```python
# adapters/driven/recovery/systemd_recovery.py
import subprocess
from devforge.domain.watchdog.recovery.strategies import RecoveryAction
from devforge.ports.recovery import RecoveryPort

class SystemdRecoveryAdapter(RecoveryPort):
    """Execute recovery actions via systemd."""
    
    def execute_recovery(self, action: RecoveryAction) -> bool:
        """Execute recovery action for systemd service."""
        if action.action_type == "restart":
            return self._restart_service(action.component)
        elif action.action_type == "reload":
            return self._reload_service(action.component)
        elif action.action_type == "reset":
            return self._hard_reset(action.component)
        else:
            return False
    
    def _restart_service(self, service: str) -> bool:
        """Soft restart: systemctl restart."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", service],
                capture_output=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _reload_service(self, service: str) -> bool:
        """Medium recovery: systemctl reload-or-restart."""
        try:
            result = subprocess.run(
                ["systemctl", "--user", "reload-or-restart", service],
                capture_output=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def _hard_reset(self, service: str) -> bool:
        """Hard reset: stop + daemon-reload + start."""
        try:
            # Stop
            subprocess.run(
                ["systemctl", "--user", "stop", service],
                capture_output=True,
                timeout=30
            )
            
            # Daemon reload
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
                timeout=30
            )
            
            # Start
            result = subprocess.run(
                ["systemctl", "--user", "start", service],
                capture_output=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False
```

3. **Write tests** (1h):
```python
# tests/unit/adapters/driven/recovery/test_systemd_recovery.py
import pytest
from unittest.mock import patch, MagicMock
from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
from devforge.domain.watchdog.recovery.strategies import RecoveryAction

class TestSystemdRecoveryAdapter:
    @patch("subprocess.run")
    def test_restart_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        
        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="restart",
            detail="Restart service",
            severity=0
        )
        
        result = adapter.execute_recovery(action)
        assert result is True
        mock_run.assert_called_once()
    
    @patch("subprocess.run")
    def test_restart_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        
        adapter = SystemdRecoveryAdapter()
        action = RecoveryAction(
            component="test-service",
            action_type="restart",
            detail="Restart service",
            severity=0
        )
        
        result = adapter.execute_recovery(action)
        assert result is False
```

---

#### Task C2: Notification Adapters (2-3h)

**Input**: `notifier.py` (376 lines)

**Output**:
- `ports/notification.py`
- `adapters/driven/notification/slack_notifier.py`
- `adapters/driven/notification/systemd_notifier.py`

**Steps**:

1. **Define port** (15min):
```python
# ports/notification.py
from typing import Protocol
from devforge.domain.watchdog.monitoring.health_status import ComponentStatus
from devforge.domain.watchdog.recovery.strategies import RecoveryAction

class NotificationPort(Protocol):
    """Port for sending notifications."""
    
    def send_alert(self, component: str, status: ComponentStatus) -> bool:
        """Send alert for component failure."""
        ...
    
    def send_recovery(self, component: str, action: RecoveryAction, success: bool) -> bool:
        """Send recovery notification."""
        ...
```

2. **Implement Slack notifier** (1.5h):
```python
# adapters/driven/notification/slack_notifier.py
import httpx
from devforge.domain.watchdog.monitoring.health_status import ComponentStatus, ComponentState
from devforge.domain.watchdog.recovery.strategies import RecoveryAction
from devforge.ports.notification import NotificationPort

class SlackNotifier(NotificationPort):
    """Send notifications to Slack."""
    
    def __init__(self, webhook_url: str):
        self._webhook_url = webhook_url
    
    def send_alert(self, component: str, status: ComponentStatus) -> bool:
        """Send alert for component failure."""
        emoji = self._state_emoji(status.state)
        message = {
            "text": f"{emoji} *Alert: {component}*",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *Alert: {component}*\n"
                            f"State: {status.state.value}\n"
                            f"Failures: {status.consecutive_failures}\n"
                            f"Circuit: {'🔴 OPEN' if status.circuit_open else '🟢 CLOSED'}"
                        )
                    }
                }
            ]
        }
        
        return self._send(message)
    
    def send_recovery(self, component: str, action: RecoveryAction, success: bool) -> bool:
        """Send recovery notification."""
        emoji = "✅" if success else "❌"
        message = {
            "text": f"{emoji} Recovery: {component}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"{emoji} *Recovery: {component}*\n"
                            f"Action: {action.action_type} (severity={action.severity})\n"
                            f"Result: {'SUCCESS' if success else 'FAILED'}\n"
                            f"Detail: {action.detail}"
                        )
                    }
                }
            ]
        }
        
        return self._send(message)
    
    def _state_emoji(self, state: ComponentState) -> str:
        mapping = {
            ComponentState.HEALTHY: "🟢",
            ComponentState.DEGRADED: "🟡",
            ComponentState.CRITICAL: "🔴",
            ComponentState.RECOVERING: "🔵",
        }
        return mapping.get(state, "⚪")
    
    def _send(self, message: dict) -> bool:
        """Send message to Slack webhook."""
        try:
            response = httpx.post(
                self._webhook_url,
                json=message,
                timeout=10
            )
            return response.status_code == 200
        except Exception:
            return False
```

3. **Implement systemd notifier** (30min):
```python
# adapters/driven/notification/systemd_notifier.py
import subprocess
from devforge.domain.watchdog.monitoring.health_status import ComponentStatus
from devforge.domain.watchdog.recovery.strategies import RecoveryAction
from devforge.ports.notification import NotificationPort

class SystemdNotifier(NotificationPort):
    """Send notifications via systemd sd_notify."""
    
    def send_alert(self, component: str, status: ComponentStatus) -> bool:
        """Send alert via sd_notify."""
        message = f"STATUS=ALERT: {component} ({status.state.value})"
        return self._notify(message)
    
    def send_recovery(self, component: str, action: RecoveryAction, success: bool) -> bool:
        """Send recovery notification via sd_notify."""
        result = "SUCCESS" if success else "FAILED"
        message = f"STATUS=RECOVERY: {component} {action.action_type} {result}"
        return self._notify(message)
    
    def _notify(self, message: str) -> bool:
        """Execute systemd-notify."""
        try:
            result = subprocess.run(
                ["systemd-notify", "--user", message],
                capture_output=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
```

4. **Write tests** (1h):
```python
# tests/unit/adapters/driven/notification/test_slack_notifier.py
import pytest
from unittest.mock import patch, MagicMock
from devforge.adapters.driven.notification.slack_notifier import SlackNotifier
from devforge.domain.watchdog.monitoring.health_status import ComponentStatus, ComponentState
from datetime import datetime, UTC

class TestSlackNotifier:
    @patch("httpx.post")
    def test_send_alert_success(self, mock_post):
        mock_post.return_value = MagicMock(status_code=200)
        
        notifier = SlackNotifier("https://hooks.slack.com/test")
        status = ComponentStatus(
            name="test-svc",
            state=ComponentState.CRITICAL,
            fail_count=5,
            consecutive_failures=5,
            circuit_open=True,
            last_success=None,
            last_failure=datetime.now(UTC)
        )
        
        result = notifier.send_alert("test-svc", status)
        assert result is True
        
        # Verify message structure
        call_args = mock_post.call_args
        message = call_args.kwargs["json"]
        assert "Alert: test-svc" in message["text"]
        assert "CRITICAL" in message["blocks"][0]["text"]["text"]
```

---

### Phase D: Storage & Application Layer (Day 4-5, 1 day)

**Goal**: Complete infrastructure adapters and wire everything together

#### Task D1: Incident Repository (2-3h)

**Input**: `incidents.py` (288 lines)

**Output**:
- `domain/watchdog/incidents/repository.py` (protocol)
- `adapters/driven/storage/incident_pg.py` (PostgreSQL implementation)

**Steps**:

1. **Define domain model** (30min):
```python
# domain/watchdog/incidents/repository.py
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

@dataclass
class Incident:
    """Domain model for watchdog incident."""
    id: int | None
    component: str
    severity: str  # "degraded", "critical"
    detail: str
    created_at: datetime
    resolved_at: datetime | None
    resolution_note: str | None

class IncidentRepository(Protocol):
    """Repository for incident persistence."""
    
    async def save(self, incident: Incident) -> Incident:
        """Save new incident."""
        ...
    
    async def resolve(self, incident_id: int, note: str) -> bool:
        """Resolve incident."""
        ...
    
    async def find_open(self, component: str | None = None) -> list[Incident]:
        """Find open incidents."""
        ...
```

2. **Implement PostgreSQL repository** (1.5h):
```python
# adapters/driven/storage/incident_pg.py
from devforge.domain.watchdog.incidents.repository import Incident, IncidentRepository
from devforge.core.database import DatabaseGateway
from datetime import datetime, UTC

class PostgreSQLIncidentRepository(IncidentRepository):
    """PostgreSQL implementation of incident repository."""
    
    def __init__(self, db: DatabaseGateway):
        self._db = db
    
    async def save(self, incident: Incident) -> Incident:
        """Save new incident."""
        query = """
            INSERT INTO watchdog_incidents 
            (component, severity, detail, created_at)
            VALUES ($1, $2, $3, $4)
            RETURNING id
        """
        
        async with self._db.session() as session:
            result = await session.execute(
                query,
                incident.component,
                incident.severity,
                incident.detail,
                incident.created_at
            )
            incident_id = result.fetchone()[0]
        
        return Incident(
            id=incident_id,
            component=incident.component,
            severity=incident.severity,
            detail=incident.detail,
            created_at=incident.created_at,
            resolved_at=None,
            resolution_note=None
        )
    
    async def resolve(self, incident_id: int, note: str) -> bool:
        """Resolve incident."""
        query = """
            UPDATE watchdog_incidents
            SET resolved_at = $1, resolution_note = $2
            WHERE id = $3 AND resolved_at IS NULL
        """
        
        async with self._db.session() as session:
            result = await session.execute(
                query,
                datetime.now(UTC),
                note,
                incident_id
            )
            return result.rowcount > 0
    
    async def find_open(self, component: str | None = None) -> list[Incident]:
        """Find open incidents."""
        if component:
            query = """
                SELECT id, component, severity, detail, created_at, resolved_at, resolution_note
                FROM watchdog_incidents
                WHERE resolved_at IS NULL AND component = $1
                ORDER BY created_at DESC
            """
            params = [component]
        else:
            query = """
                SELECT id, component, severity, detail, created_at, resolved_at, resolution_note
                FROM watchdog_incidents
                WHERE resolved_at IS NULL
                ORDER BY created_at DESC
            """
            params = []
        
        async with self._db.session() as session:
            result = await session.execute(query, *params)
            rows = result.fetchall()
        
        return [
            Incident(
                id=row[0],
                component=row[1],
                severity=row[2],
                detail=row[3],
                created_at=row[4],
                resolved_at=row[5],
                resolution_note=row[6]
            )
            for row in rows
        ]
```

3. **Write tests** (1h):
```python
# tests/unit/adapters/driven/storage/test_incident_pg.py
import pytest
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock
from devforge.adapters.driven.storage.incident_pg import PostgreSQLIncidentRepository
from devforge.domain.watchdog.incidents.repository import Incident

@pytest.mark.asyncio
async def test_save_incident():
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.fetchone.return_value = [123]
    mock_session.execute.return_value = mock_result
    mock_db.session.return_value.__aenter__.return_value = mock_session
    
    repo = PostgreSQLIncidentRepository(mock_db)
    incident = Incident(
        id=None,
        component="test-svc",
        severity="critical",
        detail="Service down",
        created_at=datetime.now(UTC),
        resolved_at=None,
        resolution_note=None
    )
    
    saved = await repo.save(incident)
    assert saved.id == 123
    assert saved.component == "test-svc"
```

---

#### Task D2: Watchdog Application Service (3-4h)

**Input**: `orchestrator.py` main loop

**Output**: `application/watchdog_service.py`

**Steps**:

1. **Wire all components** (2h):
```python
# application/watchdog_service.py
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker
from devforge.domain.watchdog.monitoring.circuit_breaker import CircuitBreaker
from devforge.domain.watchdog.orchestration.check_coordinator import CheckCoordinator, CheckPlan
from devforge.domain.watchdog.orchestration.fix_coordinator import FixCoordinator
from devforge.domain.watchdog.recovery.graduation import RecoveryCoordinator
from devforge.domain.watchdog.incidents.repository import IncidentRepository, Incident
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.recovery import RecoveryPort
from devforge.ports.notification import NotificationPort
from devforge.core.config import WatchdogConfig
from datetime import datetime, UTC

class WatchdogService:
    """Application service for watchdog orchestration.
    
    Coordinates health monitoring, recovery, and notification.
    """
    
    def __init__(
        self,
        config: WatchdogConfig,
        health_ports: dict[str, HealthCheckPort],
        recovery_ports: dict[str, RecoveryPort],
        notification_ports: list[NotificationPort],
        incident_repo: IncidentRepository,
    ):
        # Core domain
        self._tracker = ComponentTracker(
            failure_threshold=config.failure_threshold,
            success_threshold=config.success_threshold
        )
        self._circuit_breaker = CircuitBreaker(self._tracker)
        
        # Recovery
        self._recovery_coordinator = RecoveryCoordinator(self._tracker)
        
        # Orchestration
        self._check_coordinator = CheckCoordinator(self._tracker, health_ports)
        self._fix_coordinator = FixCoordinator(
            self._recovery_coordinator,
            recovery_ports
        )
        
        # Infrastructure
        self._notification_ports = notification_ports
        self._incident_repo = incident_repo
        
        # Config
        self._config = config
    
    async def run_check_cycle(self) -> dict:
        """Execute one complete check-fix cycle.
        
        Returns:
            Summary dict with check/fix results.
        """
        # 1. Execute health checks
        plan = CheckPlan(
            components=list(self._config.critical_services),
            parallel=True,
            timeout_per_check=self._config.check_timeout_sec
        )
        
        check_results = self._check_coordinator.execute_checks(plan)
        
        # 2. Identify failures
        failed = [r.component for r in check_results if not r.ok]
        
        # 3. Execute fixes
        fix_results = {}
        if failed:
            fix_results = self._fix_coordinator.execute_fixes(failed)
        
        # 4. Create incidents for new failures
        for component in failed:
            status = self._tracker.get_status(component)
            if status and status.state == ComponentState.CRITICAL:
                # Check if already open incident
                open_incidents = await self._incident_repo.find_open(component)
                if not open_incidents:
                    incident = Incident(
                        id=None,
                        component=component,
                        severity=status.state.value,
                        detail=f"Consecutive failures: {status.consecutive_failures}",
                        created_at=datetime.now(UTC),
                        resolved_at=None,
                        resolution_note=None
                    )
                    await self._incident_repo.save(incident)
        
        # 5. Send notifications
        for component in failed:
            status = self._tracker.get_status(component)
            if status:
                for notifier in self._notification_ports:
                    notifier.send_alert(component, status)
        
        # 6. Notify recovery results
        for component, success in fix_results.items():
            action = self._recovery_coordinator.plan_recovery(component)
            if action:
                for notifier in self._notification_ports:
                    notifier.send_recovery(component, action, success)
        
        return {
            "checks": len(check_results),
            "failed": len(failed),
            "fixed": sum(1 for v in fix_results.values() if v),
            "timestamp": datetime.now(UTC).isoformat()
        }
    
    async def resolve_incident(self, incident_id: int, note: str) -> bool:
        """Manually resolve an incident."""
        return await self._incident_repo.resolve(incident_id, note)
    
    def get_component_status(self, component: str):
        """Get current status of component."""
        return self._tracker.get_status(component)
    
    def get_all_statuses(self):
        """Get status of all tracked components."""
        return self._tracker.all_statuses()
```

2. **Factory function** (30min):
```python
# application/watchdog_service.py (continued)

async def create_watchdog_service(config: WatchdogConfig) -> WatchdogService:
    """Factory function to create fully-wired WatchdogService."""
    from devforge.adapters.driven.health.systemd_health import (
        SystemdServiceHealthCheck,
        SystemdTimerHealthCheck
    )
    from devforge.adapters.driven.health.llm_health import LLMHealthCheck
    from devforge.adapters.driven.health.system_health import (
        MemoryHealthCheck,
        DiskHealthCheck
    )
    from devforge.adapters.driven.recovery.systemd_recovery import SystemdRecoveryAdapter
    from devforge.adapters.driven.notification.slack_notifier import SlackNotifier
    from devforge.adapters.driven.notification.systemd_notifier import SystemdNotifier
    from devforge.adapters.driven.storage.incident_pg import PostgreSQLIncidentRepository
    from devforge.core.database import DatabaseGateway
    from devforge.core.config import get_config
    
    # Health check ports
    health_ports = {
        "systemd-services": SystemdServiceHealthCheck(config.critical_services),
        "systemd-timers": SystemdTimerHealthCheck([
            "devforge-day-cycle.timer",
            "devforge-night-cycle.timer"
        ]),
        "llm-pods": LLMHealthCheck(config.llm_targets),
        "memory": MemoryHealthCheck(threshold_percent=90.0),
        "disk-root": DiskHealthCheck("/", threshold_percent=90.0),
        "disk-data": DiskHealthCheck("/opt/ai_data", threshold_percent=90.0),
    }
    
    # Recovery ports
    recovery_ports = {
        svc: SystemdRecoveryAdapter()
        for svc in config.critical_services
    }
    
    # Notification ports
    app_config = get_config()
    notification_ports = [
        SystemdNotifier(),
    ]
    if app_config.slack_webhook:
        notification_ports.append(SlackNotifier(app_config.slack_webhook))
    
    # Incident repository
    db = DatabaseGateway(app_config.db_url())
    incident_repo = PostgreSQLIncidentRepository(db)
    
    return WatchdogService(
        config=config,
        health_ports=health_ports,
        recovery_ports=recovery_ports,
        notification_ports=notification_ports,
        incident_repo=incident_repo
    )
```

3. **Write integration tests** (1.5h):
```python
# tests/unit/application/test_watchdog_service.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from devforge.application.watchdog_service import WatchdogService
from devforge.core.config import WatchdogConfig
from devforge.domain.watchdog.monitoring.health_status import HealthCheckResult
from datetime import datetime, UTC

@pytest.mark.asyncio
async def test_check_cycle_all_healthy():
    config = WatchdogConfig()
    
    # Mock health port
    mock_health_port = MagicMock()
    mock_health_port.check_health.return_value = HealthCheckResult(
        component="svc1",
        ok=True,
        detail="OK",
        timestamp=datetime.now(UTC)
    )
    
    health_ports = {"svc1": mock_health_port}
    recovery_ports = {}
    notification_ports = []
    
    mock_incident_repo = AsyncMock()
    
    service = WatchdogService(
        config=config,
        health_ports=health_ports,
        recovery_ports=recovery_ports,
        notification_ports=notification_ports,
        incident_repo=mock_incident_repo
    )
    
    result = await service.run_check_cycle()
    
    assert result["checks"] == 1
    assert result["failed"] == 0
    assert result["fixed"] == 0

@pytest.mark.asyncio
async def test_check_cycle_with_failure_and_recovery():
    config = WatchdogConfig()
    
    # Mock health port (fails)
    mock_health_port = MagicMock()
    mock_health_port.check_health.return_value = HealthCheckResult(
        component="svc1",
        ok=False,
        detail="Connection refused",
        timestamp=datetime.now(UTC)
    )
    
    # Mock recovery port (succeeds)
    mock_recovery_port = MagicMock()
    mock_recovery_port.execute_recovery.return_value = True
    
    # Mock notification
    mock_notifier = MagicMock()
    
    # Mock incident repo
    mock_incident_repo = AsyncMock()
    mock_incident_repo.find_open.return_value = []
    mock_incident_repo.save.return_value = MagicMock(id=1)
    
    health_ports = {"svc1": mock_health_port}
    recovery_ports = {"svc1": mock_recovery_port}
    notification_ports = [mock_notifier]
    
    service = WatchdogService(
        config=config,
        health_ports=health_ports,
        recovery_ports=recovery_ports,
        notification_ports=notification_ports,
        incident_repo=mock_incident_repo
    )
    
    result = await service.run_check_cycle()
    
    assert result["checks"] == 1
    assert result["failed"] == 1
    # Note: May not fix on first failure (depends on threshold)
```

---

#### Task D3: CLI Integration (1-2h)

**Input**: Legacy `orchestrator.py` CLI entry points

**Output**: `adapters/driving/cli_cmds/watchdog.py`

**Steps**:

1. **Implement CLI commands** (1h):
```python
# adapters/driving/cli_cmds/watchdog.py
import asyncio
from devforge.application.watchdog_service import create_watchdog_service
from devforge.core.config import WatchdogConfig

async def watchdog_check():
    """Run one check-fix cycle."""
    config = WatchdogConfig.from_env()
    service = await create_watchdog_service(config)
    
    result = await service.run_check_cycle()
    
    print(f"Checks: {result['checks']}")
    print(f"Failed: {result['failed']}")
    print(f"Fixed: {result['fixed']}")
    print(f"Timestamp: {result['timestamp']}")

async def watchdog_status():
    """Show status of all components."""
    config = WatchdogConfig.from_env()
    service = await create_watchdog_service(config)
    
    statuses = service.get_all_statuses()
    
    for status in statuses:
        print(f"{status.name}: {status.state.value} "
              f"(fail_count={status.fail_count}, "
              f"circuit={'OPEN' if status.circuit_open else 'CLOSED'})")

async def watchdog_resolve(incident_id: int, note: str):
    """Resolve an incident."""
    config = WatchdogConfig.from_env()
    service = await create_watchdog_service(config)
    
    success = await service.resolve_incident(incident_id, note)
    
    if success:
        print(f"Incident {incident_id} resolved")
    else:
        print(f"Failed to resolve incident {incident_id}")

# CLI entry points
def cli_watchdog_check(args):
    """CLI: watchdog check"""
    asyncio.run(watchdog_check())

def cli_watchdog_status(args):
    """CLI: watchdog status"""
    asyncio.run(watchdog_status())

def cli_watchdog_resolve(args):
    """CLI: watchdog resolve <id> <note>"""
    asyncio.run(watchdog_resolve(args.incident_id, args.note))
```

2. **Wire into cli.py** (30min):
```python
# cli.py (extend existing)
from adapters.driving.cli_cmds.watchdog import (
    cli_watchdog_check,
    cli_watchdog_status,
    cli_watchdog_resolve
)

# Add subcommand
watchdog_parser = subparsers.add_parser("watchdog", help="Watchdog operations")
watchdog_sub = watchdog_parser.add_subparsers(dest="watchdog_cmd")

# watchdog check
check_parser = watchdog_sub.add_parser("check", help="Run check-fix cycle")
check_parser.set_defaults(func=cli_watchdog_check)

# watchdog status
status_parser = watchdog_sub.add_parser("status", help="Show component status")
status_parser.set_defaults(func=cli_watchdog_status)

# watchdog resolve
resolve_parser = watchdog_sub.add_parser("resolve", help="Resolve incident")
resolve_parser.add_argument("incident_id", type=int)
resolve_parser.add_argument("note", type=str)
resolve_parser.set_defaults(func=cli_watchdog_resolve)
```

3. **Write CLI tests** (30min):
```bash
# Manual verification
python3 cli.py watchdog status
python3 cli.py watchdog check
```

---

## Verification Gates

### Gate 1: Domain Logic (After Phase A)

```bash
# Unit tests
pytest tests/unit/domain/watchdog/ -v

# Expected: 30+ tests PASSED
# Coverage target: >90% for domain logic

# import-linter
lint-imports

# Expected: 4 contracts KEPT (no violations)
```

### Gate 2: Adapters (After Phase B+C)

```bash
# Unit tests
pytest tests/unit/adapters/ -v

# Expected: 40+ tests PASSED

# Parity tests
pytest tests/characterization/test_watchdog_*_parity.py -v

# Expected: ALL parity tests PASSED
```

### Gate 3: Integration (After Phase D)

```bash
# Integration tests
pytest tests/unit/application/ -v

# Expected: 10+ tests PASSED

# CLI smoke test
python3 cli.py watchdog status
python3 cli.py watchdog check

# Systemd service test
systemctl --user start devforge-watchdog.service
sleep 60
systemctl --user status devforge-watchdog.service
# Expected: active (running)
```

### Gate 4: Production Switchover

**Prerequisites**:
1. All tests passing (unit + characterization + integration)
2. import-linter clean
3. Manual smoke test completed
4. Backup of legacy orchestrator.py

**Switchover Steps**:
```bash
# 1. Deploy new systemd service
cp systemd/user/devforge-watchdog-new.service ~/.config/systemd/user/devforge-watchdog.service
systemctl --user daemon-reload

# 2. Stop legacy
systemctl --user stop devforge-watchdog-legacy.service

# 3. Start new
systemctl --user start devforge-watchdog.service

# 4. Monitor for 1 hour
watch -n 60 'systemctl --user status devforge-watchdog.service'

# 5. Check logs
journalctl --user -u devforge-watchdog.service -n 100

# 6. Rollback if issues
systemctl --user stop devforge-watchdog.service
systemctl --user start devforge-watchdog-legacy.service
```

---

## Commit Strategy

Each task = 1 commit (20 commits total):

```
Phase A: Core Domain (4 commits)
- feat(watchdog): add state tracker and circuit breaker [A1]
- feat(watchdog): add recovery strategies and graduation [A2]
- feat(watchdog): add check/fix coordinators [A3]
- feat(watchdog): migrate config to core [A4]

Phase B: Health Adapters (4 commits)
- feat(watchdog): add systemd health adapter [B1]
- feat(watchdog): add LLM health adapter [B2]
- feat(watchdog): add system health adapter [B3]
- feat(watchdog): add pipeline health adapter [B4]

Phase C: Recovery & Notification (4 commits)
- feat(watchdog): add systemd recovery adapter [C1]
- feat(watchdog): add Slack notifier [C2a]
- feat(watchdog): add systemd notifier [C2b]
- test(watchdog): add parity tests for Phase B+C [C3]

Phase D: Application Layer (4 commits)
- feat(watchdog): add incident repository [D1]
- feat(watchdog): add watchdog application service [D2]
- feat(watchdog): integrate CLI commands [D3]
- test(watchdog): add integration tests [D4]

Deployment (4 commits)
- chore(watchdog): add new systemd service unit [Deploy1]
- docs(watchdog): update architecture docs [Deploy2]
- chore(watchdog): production switchover [Deploy3]
- chore(watchdog): archive legacy watchdog code [Deploy4]
```

---

## Risk Mitigation

### Risk 1: State Machine Logic Drift

**Mitigation**: Characterization tests that compare new tracker state transitions against legacy behavior

### Risk 2: Recovery Action Breakage

**Mitigation**: Shadow mode deployment (run new + legacy in parallel for 24h)

### Risk 3: Notification Failure

**Mitigation**: Notification adapters return bool success; log failures to DB

### Risk 4: Database Migration

**Mitigation**: `watchdog_incidents` table already exists; no migration needed

### Risk 5: Performance Regression

**Mitigation**: Profile check cycle time (legacy baseline: ~15s, target: <20s)

---

## Success Criteria

1. **Functionality**: All legacy watchdog features work via new architecture
2. **Tests**: >80% coverage, all parity tests pass
3. **Architecture**: import-linter 4 contracts KEPT
4. **Performance**: Check cycle time <20s (legacy: ~15s)
5. **Deployment**: Zero downtime switchover
6. **Monitoring**: No Slack alerts for 48h post-deployment

---

## Appendix A: Legacy Function Mapping

| Legacy Function | New Location | Notes |
|----------------|--------------|-------|
| `orchestrator.run_check_cycle` | `application.watchdog_service.WatchdogService.run_check_cycle` | Main entry |
| `checker.check_all_services` | `adapters.driven.health.systemd_health.SystemdServiceHealthCheck` | Health port |
| `checker.check_all_llm` | `adapters.driven.health.llm_health.LLMHealthCheck` | Health port |
| `checker.check_memory` | `adapters.driven.health.system_health.MemoryHealthCheck` | Health port |
| `state.ComponentTracker` | `domain.watchdog.monitoring.tracker.ComponentTracker` | Domain logic |
| `recovery.graduated_recover` | `domain.watchdog.recovery.graduation.RecoveryCoordinator` | Domain logic |
| `recovery.restart_service` | `adapters.driven.recovery.systemd_recovery.SystemdRecoveryAdapter._restart_service` | Recovery port |
| `notifier.send_alert` | `adapters.driven.notification.slack_notifier.SlackNotifier.send_alert` | Notification port |
| `incidents.create_incident` | `domain.watchdog.incidents.repository.IncidentRepository.save` | Domain repo |
| `config.FAILURE_THRESHOLD` | `core.config.WatchdogConfig.failure_threshold` | Config |

---

## Appendix B: Test Coverage Target

| Module | Unit Tests | Characterization Tests | Target Coverage |
|--------|------------|----------------------|-----------------|
| `domain/watchdog/monitoring` | 12 | 0 | >90% |
| `domain/watchdog/recovery` | 8 | 0 | >90% |
| `domain/watchdog/orchestration` | 6 | 0 | >85% |
| `adapters/driven/health` | 16 | 4 | >80% |
| `adapters/driven/recovery` | 4 | 2 | >80% |
| `adapters/driven/notification` | 8 | 0 | >75% |
| `adapters/driven/storage` | 6 | 0 | >80% |
| `application/watchdog_service` | 10 | 0 | >85% |
| **Total** | **70** | **6** | **>85%** |

---

## Appendix C: Environment Variables

```bash
# Watchdog configuration
WATCHDOG_FAILURE_THRESHOLD=3
WATCHDOG_SUCCESS_THRESHOLD=2
WATCHDOG_CIRCUIT_RESET_SEC=300
WATCHDOG_CHECK_INTERVAL_SEC=60
WATCHDOG_CHECK_TIMEOUT_SEC=30

# Database (existing)
DEVFORGE_DB_URL=postgresql://devforge:***@localhost/devforge_app

# Slack (existing)
DEVFORGE_SLACK_WEBHOOK=https://hooks.slack.com/services/***

# Paths (existing)
DEVFORGE_DATA_DIR=/opt/ai_data
DEVFORGE_SERVER_DIR=/opt/projects/server
```

---

## End of Phase 2 Detailed Guide