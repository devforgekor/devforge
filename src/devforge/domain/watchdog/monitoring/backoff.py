#!/usr/bin/env python3
# Status: experimental
# Path: domain/watchdog/monitoring/
"""CrashLoopBackOff with jitter (legacy state.py:160-171, config.py:126)."""
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
