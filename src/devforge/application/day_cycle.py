#!/usr/bin/env python3.12
# Status: experimental
# Path: devforge.cli (pipeline orchestrate)
"""Day cycle — owned-stage orchestration (D6=A: FTS5 refresh + embed)."""

from __future__ import annotations

from typing import Callable, Optional

from devforge.application.orchestrator import Budget, PipelineOrchestrator, PipelineStage
from devforge.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BUDGET_SEC = 3600


class Fts5RefreshStage:
    """Thin stage wrapping the FTS5 refresh callable."""

    name = "fts5_refresh"

    def __init__(self, refresh: Callable[[], str]) -> None:
        self._refresh = refresh

    def run(self) -> str:
        try:
            return self._refresh()
        except Exception as exc:
            # [WHY] day_cycle.sh continues after FTS5 failure — mirror that
            # behavior instead of aborting the embed stage (behavior parity).
            logger.warning("fts5_refresh_failed", error=str(exc))
            return f"fts5_refresh FAILED: {exc}"


def run_full_cycle(
    *,
    embed_stage: PipelineStage,
    fts5_refresh: Optional[Callable[[], str]] = None,
    budget_sec: int = DEFAULT_BUDGET_SEC,
) -> list[str]:
    """Run owned stages in order under a shared budget (D6=A scope)."""
    stages: list[PipelineStage] = []
    if fts5_refresh is not None:
        stages.append(Fts5RefreshStage(fts5_refresh))
    stages.append(embed_stage)
    return PipelineOrchestrator(stages, Budget(budget_sec)).run()
