#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/monitoring/
"""Component state tracker, registry, and circuit breaker (legacy state.py:79-232).

Also houses TrendTracker (legacy state.py:28-77).
"""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

from devforge.domain.watchdog.monitoring.backoff import BackoffCalculator
from devforge.ports.types import CircuitState, ComponentState, HealthCheck

CIRCUIT_BREAKER_TIMEOUT = 120  # config.py:129
BACKOFF_RESET_SEC = 600  # config.py:128
DEFAULT_ALERT_DEDUP_SEC = 300  # state.py:148
TREND_MAX_SAMPLES = 60  # state.py:38


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
        self.next_attempt_at = 0.0
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
        return self.state in (
            ComponentState.DEGRADED,
            ComponentState.UNHEALTHY,
            ComponentState.DOWN,
        )

    def can_attempt_recovery(self) -> bool:
        """Non-blocking backoff gate: recovery may run once next_attempt_at passes."""
        return time.monotonic() >= self.next_attempt_at

    def schedule_next_attempt(self, backoff_sec: int) -> None:
        """Defer the next recovery attempt by `backoff_sec` (no blocking sleep)."""
        self.next_attempt_at = time.monotonic() + backoff_sec

    def backoff_sec(self) -> int:
        return self._backoff.calculate(self.consecutive_fail)

    def circuit_status(self) -> CircuitState:
        is_open = self.circuit_open_until > time.monotonic()
        return CircuitState(
            is_open=is_open,
            failure_count=self.consecutive_fail,
            opens_at=self.circuit_open_until if is_open else None,
            can_retry=self.can_retry(),
        )

    # ── persistence ─────────────────────────────────────────────────
    def summary(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "fail_count": self.fail_count,
            "consecutive_fail": self.consecutive_fail,
            "circuit_open": self.circuit_open_until > time.monotonic(),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "fail_count": self.fail_count,
            "consecutive_fail": self.consecutive_fail,
            "last_state_change": self.last_state_change,
            "last_alert_ts": self.last_alert_ts,
            "last_success_ts": self.last_success_ts,
            "last_fail_ts": self.last_fail_ts,
            "circuit_open_until": self.circuit_open_until,
            "next_attempt_at": self.next_attempt_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "ComponentTracker":
        t = cls(str(data.get("name", "unknown")))
        try:
            t.state = ComponentState(data.get("state", "HEALTHY"))
        except ValueError:
            t.state = ComponentState.HEALTHY
        t.fail_count = int(data.get("fail_count", 0))  # type: ignore[call-overload]
        t.consecutive_fail = int(data.get("consecutive_fail", 0))  # type: ignore[call-overload]
        t.last_state_change = float(data.get("last_state_change", 0.0))  # type: ignore[arg-type]
        t.last_alert_ts = float(data.get("last_alert_ts", 0.0))  # type: ignore[arg-type]
        t.last_success_ts = float(data.get("last_success_ts", time.monotonic()))  # type: ignore[arg-type]
        t.last_fail_ts = float(data.get("last_fail_ts", 0.0))  # type: ignore[arg-type]
        t.circuit_open_until = float(data.get("circuit_open_until", 0.0))  # type: ignore[arg-type]
        t.next_attempt_at = float(data.get("next_attempt_at", 0.0))  # type: ignore[arg-type]
        return t

    def _transition(self, new_state: ComponentState) -> None:
        if self.state != new_state:
            self.state = new_state
            self.last_state_change = time.monotonic()


class TrackerRegistry:
    """Aggregate of per-key trackers (legacy WatchdogState._components, state.py:229)."""

    def __init__(self) -> None:
        self._trackers: dict[str, ComponentTracker] = {}

    def get(self, key: str) -> ComponentTracker:
        if key not in self._trackers:
            self._trackers[key] = ComponentTracker(key)
        return self._trackers[key]

    def all(self) -> dict[str, ComponentTracker]:
        return dict(self._trackers)

    def restore(self, data: dict[str, object]) -> None:
        for key, raw in (data or {}).items():
            try:
                self._trackers[key] = ComponentTracker.from_dict(raw)  # type: ignore[arg-type]
            except Exception:
                continue


class TrendTracker:
    """Metric trend + linear-regression ETA (legacy state.py:28-77)."""

    def __init__(self, max_samples: int = TREND_MAX_SAMPLES) -> None:
        self.max_samples = max_samples
        self._data: List[Tuple[float, float]] = []  # (monotonic_sec, value)

    def add(self, value: float, timestamp: Optional[float] = None) -> None:
        """Record a sample. Timestamp defaults to time.monotonic()."""
        ts = time.monotonic() if timestamp is None else timestamp
        self._data.append((ts, value))
        if len(self._data) > self.max_samples:
            self._data.pop(0)

    def predict_eta(self, threshold: float) -> Optional[float]:
        """Minutes until threshold breach; None if <10 samples, flat, or descending."""
        if len(self._data) < 10:
            return None
        xs = [t - self._data[0][0] for t, _ in self._data]
        ys = [v for _, v in self._data]
        n = len(xs)
        sx = sum(xs)
        sy = sum(ys)
        sxx = sum(x * x for x in xs)
        sxy = sum(x * y for x, y in zip(xs, ys))
        denom = n * sxx - sx * sx
        if denom == 0:
            return None
        slope = (n * sxy - sx * sy) / denom
        if slope <= 0:
            return None  # ← slope first (legacy order)
        latest = ys[-1]
        if threshold <= latest:
            return 0.0  # ← breach second
        return (threshold - latest) / slope / 60.0

    def latest(self) -> Optional[float]:
        return self._data[-1][1] if self._data else None

    def clear(self) -> None:
        self._data.clear()
