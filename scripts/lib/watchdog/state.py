# Status: production
# Path: imported by — watchdog.py, fixloop.py
"""Component state machine — HEALTHY↔DEGRADED↔UNHEALTHY↔DOWN.

Circuit breaker pattern (pyresilience, pybreaker):
  CLOSED (정상) → 실패 감지 → OPEN (차단)
  OPEN → timeout → HALF_OPEN (테스트)
  HALF_OPEN → 성공 → CLOSED / 실패 → OPEN
"""

import time
from datetime import datetime, timezone
from enum import Enum


class ComponentState(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"    # 1-2 failures, still retrying
    UNHEALTHY = "UNHEALTHY"  # 3+ failures, circuit open
    DOWN = "DOWN"            # 5+ failures, escalated


class ComponentTracker:
    """Tracks state + failure count for one component.

    CrashLoopBackOff reset: if healthy for BACKOFF_RESET_SEC → reset counter.
    """

    __slots__ = ("name", "state", "fail_count", "consecutive_fail",
                 "last_state_change", "last_alert_ts", "last_success_ts",
                 "last_fail_ts", "circuit_open_until")

    def __init__(self, name: str):
        self.name = name
        self.state = ComponentState.HEALTHY
        self.fail_count = 0
        self.consecutive_fail = 0
        self.last_state_change = 0.0
        self.last_alert_ts = 0.0
        self.last_success_ts = time.monotonic()
        self.last_fail_ts = 0.0
        self.circuit_open_until = 0.0

    def record_success(self):
        """Reset consecutive counter; if healthy long enough, reset all."""
        now = time.monotonic()
        self.consecutive_fail = 0
        self.last_success_ts = now

        # CrashLoopBackOff reset: 10min 정상 → 전체 리셋
        if self.fail_count > 0 and (now - self.last_fail_ts) >= 600:
            self.fail_count = 0

        self._transition(ComponentState.HEALTHY)
        self.circuit_open_until = 0.0

    def record_failure(self) -> bool:
        """Record failure, update state, return True if state changed."""
        from lib.watchdog.config import BACKOFF_RESET_SEC

        now = time.monotonic()
        self.consecutive_fail += 1
        self.fail_count += 1
        self.last_fail_ts = now

        old = self.state
        if self.consecutive_fail >= 5:
            self._transition(ComponentState.DOWN)
        elif self.consecutive_fail >= 3:
            self._transition(ComponentState.UNHEALTHY)
            # Circuit breaker OPEN
            from lib.watchdog.config import CIRCUIT_BREAKER_TIMEOUT
            self.circuit_open_until = now + CIRCUIT_BREAKER_TIMEOUT
        else:
            self._transition(ComponentState.DEGRADED)

        return old != self.state

    def can_retry(self) -> bool:
        """Circuit breaker: check if OPEN."""
        if self.circuit_open_until == 0.0:
            return True
        if time.monotonic() >= self.circuit_open_until:
            self.circuit_open_until = 0.0
            return True  # HALF_OPEN → allow one test
        return False

    def is_degraded(self) -> bool:
        return self.state in (ComponentState.DEGRADED,
                              ComponentState.UNHEALTHY,
                              ComponentState.DOWN)

    def can_alert(self, dedup_sec: int = 300) -> bool:
        now = time.monotonic()
        if now - self.last_alert_ts >= dedup_sec:
            self.last_alert_ts = now
            return True
        return False

    def _transition(self, new_state: ComponentState):
        if self.state != new_state:
            self.state = new_state
            self.last_state_change = time.monotonic()

    def backoff_sec(self) -> int:
        """Return current backoff delay based on attempt count (CrashLoopBackOff)."""
        from lib.watchdog.config import BACKOFF_SCHEDULE
        idx = min(self.consecutive_fail, len(BACKOFF_SCHEDULE) - 1)
        return BACKOFF_SCHEDULE[idx]

    def summary(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "fail_count": self.fail_count,
            "consecutive_fail": self.consecutive_fail,
            "circuit_open": self.circuit_open_until > time.monotonic(),
        }


class WatchdogState:
    """Aggregate state for all tracked components."""

    def __init__(self):
        self._components: dict[str, ComponentTracker] = {}
        self._mode = "day"
        self._last_heartbeat_ts = 0.0
        self._events: list[dict] = []  # rolling buffer, max 1000

    def get(self, name: str) -> ComponentTracker:
        if name not in self._components:
            self._components[name] = ComponentTracker(name)
        return self._components[name]

    def set_mode(self, mode: str):
        self._mode = mode

    @property
    def mode(self) -> str:
        return self._mode

    def should_heartbeat(self, interval: int = 1800) -> bool:
        now = time.monotonic()
        if now - self._last_heartbeat_ts >= interval:
            utc_now = datetime.now(timezone.utc)
            if utc_now.minute % 30 == 15:
                self._last_heartbeat_ts = now
                return True
        return False

    def add_event(self, component: str, event_type: str, detail: str,
                  from_state: str = "", to_state: str = "", fail_count: int = 0):
        self._events.append({
            "timestamp": time.monotonic(),
            "component": component,
            "type": event_type,
            "detail": detail,
        })
        # Trim to 1000
        if len(self._events) > 1000:
            self._events = self._events[-1000:]

        # Persist to DB (best-effort, non-blocking)
        try:
            from lib.db import psql_ok, esc_sql
            c = esc_sql(component)
            et = esc_sql(event_type)
            d = esc_sql(detail)
            from_st = esc_sql(from_state)
            to_st = esc_sql(to_state)
            psql_ok(
                f"INSERT INTO catchdog_events "
                f"(component, event_type, from_state, to_state, detail, fail_count) "
                f"VALUES ('{c}', '{et}', NULLIF('{from_st}', ''), NULLIF('{to_st}', ''), "
                f"NULLIF('{d}', ''), {fail_count})",
                timeout=5,
            )
        except Exception:
            pass  # best-effort — DB down shouldn't crash watchdog

    def events_since(self, sec: int) -> list[dict]:
        cutoff = time.monotonic() - sec
        return [e for e in self._events if e["timestamp"] > cutoff]

    def all_summaries(self) -> list[dict]:
        return [t.summary() for t in self._components.values()]

    def degraded_count(self) -> int:
        return sum(1 for t in self._components.values() if t.is_degraded())

    def update_liveness(self) -> None:
        """Update watchdog_main liveness timestamp in DB (dead man's switch)."""
        try:
            from lib.db import psql_ok
            psql_ok(
                "INSERT INTO watchdog_liveness (component, liveness_ts) "
                "VALUES ('watchdog_main', now()) "
                "ON CONFLICT (component) DO UPDATE SET liveness_ts = now()",
                timeout=5,
            )
        except Exception:
            pass  # best-effort — DB down shouldn't crash watchdog
