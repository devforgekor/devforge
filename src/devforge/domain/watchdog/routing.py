#!/usr/bin/env python3.12
# Status: experimental
# Path: application/controllers.py, domain/watchdog/recovery/strategies.py
"""A/B/C routing (detection-remediation-implementation-guide §3).

Purely classifies an incident into a remediation logic:
  A `fix`     — mutating recovery (existing RecoveryPort)
  B `catchup` — run a missed oneshot / kick a stale timer (CatchupPort)
  alert       — no action (alert-only families)
Kind is delegated to `strategies.classify_recovery_kind` to keep a single
recovery-kind SSOT (no live behavior change; S1 — wiring is gated S2/S3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from devforge.domain.watchdog.recovery.strategies import classify_recovery_kind

Logic = Literal["fix", "catchup", "alert"]
Impact = Literal["mutating", "non-mutating", "undetermined"]

# [WHY] terminal = retry is meaningless (permission/config errors). Initial
# heuristic list (guide §9.3) — confirm from field data before relying on it.
_TERMINAL_MARKERS = ("permission denied", "not found", "no such file")

_CATCHUP_PREFIXES = ("oneshot:", "timer:")

# [WHY] Canary stages follow Google SRE: start small, expand gradually, one at a time.
# Stage 0 = canary paths only; Stage 1 = canary + one real service; Stage 2 = all.
# Promotion requires evidence (no incidents for a period) — not time-based alone.
CANARY_STAGES: dict[int, dict[str, Sequence[str]]] = {
    0: {
        "fix": ("svc:svc-pod-forwarding",),
        "catchup": ("timer:devforge-system-sync.timer",),
    },
    1: {
        "fix": ("svc:svc-pod-forwarding", "svc:ebooklib-pipeline"),
        "catchup": ("timer:devforge-system-sync.timer", "oneshot:devforge-backup.service"),
    },
    2: {
        "fix": ("svc:",),
        "catchup": ("oneshot:", "timer:"),
    },
}


@dataclass(frozen=True)
class RouteDecision:
    logic: Logic
    kind: Optional[str]
    impact: Impact
    terminal: bool = False


def route(component: str, event_type: str, detail: str = "") -> RouteDecision:
    """Classify a failed component into a remediation logic (pure, no I/O)."""
    kind = classify_recovery_kind(component)
    if kind is None:
        return RouteDecision("alert", None, "non-mutating")
    if component.startswith(_CATCHUP_PREFIXES):
        catchup_kind = "oneshot_run" if component.startswith("oneshot:") else "timer_kick"
        return RouteDecision("catchup", catchup_kind, "mutating")
    terminal = any(marker in detail.lower() for marker in _TERMINAL_MARKERS)
    return RouteDecision("fix", kind, "mutating", terminal)


def matches_canary(component: str, canary: Sequence[str]) -> bool:
    """True if `component` is in the canary allowlist (exact or prefix match)."""
    return any(component == entry or component.startswith(entry) for entry in canary)


def canary_allowed(component: str, logic: str, stage: int) -> bool:
    """True if `component` is allowed at the given canary stage (Google SRE pattern)."""
    stage_map = CANARY_STAGES.get(stage, CANARY_STAGES[0])
    allowed = stage_map.get(logic, ())
    return any(component == entry or component.startswith(entry) for entry in allowed)
