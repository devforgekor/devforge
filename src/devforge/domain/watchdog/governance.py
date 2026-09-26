#!/usr/bin/env python3
# Status: experimental
# Path: application/controllers.py, application/watchdog_service.py
"""Remediation governance (detection-remediation-implementation-guide §5, S4).

Repeated-failure escalation: after `fail_count >= max_attempts` the auto-action
stops and a human is asked (HITL). Pure helpers — no I/O.
"""

from __future__ import annotations

MAX_ATTEMPTS_DEFAULT = 5


def escalate_needed(fail_count: int, max_attempts: int) -> bool:
    """True when a component has failed enough times to require human review."""
    return max_attempts > 0 and fail_count >= max_attempts
