#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, pipeline orchestration, tests
"""Pipeline orchestrator skeleton with budget management."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, Sequence

_KST = timezone(timedelta(hours=9))


class PipelineBudgetError(Exception):
    """Raised when a pipeline stage exceeds its time budget."""


@dataclass(frozen=True)
class Budget:
    """Time budget for a single pipeline stage."""

    limit_sec: int
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=_KST))

    def remaining(self) -> float:
        elapsed = (datetime.now(tz=_KST) - self.started_at).total_seconds()
        return max(0.0, self.limit_sec - elapsed)

    def expired(self) -> bool:
        return self.remaining() <= 0

    def gate(self) -> None:
        """Raise PipelineBudgetError if budget is expired."""
        if self.expired():
            raise PipelineBudgetError(f"Budget of {self.limit_sec}s exceeded")


class PipelineStage(Protocol):
    """Protocol for a single pipeline stage."""

    name: str

    def run(self) -> str: ...


class PipelineOrchestrator:
    """Run a sequence of pipeline stages within a shared budget."""

    def __init__(self, stages: Sequence[PipelineStage], budget: Budget) -> None:
        self._stages = list(stages)
        self._budget = budget

    def run(self) -> list[str]:
        """Execute all stages, stopping on budget expiry."""
        results: list[str] = []
        for stage in self._stages:
            self._budget.gate()
            result = stage.run()
            results.append(result)
        return results
