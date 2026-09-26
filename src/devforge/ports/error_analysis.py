#!/usr/bin/env python3.12
# Status: experimental
# Path: application/error_analysis.py (wired via cli.py composition root)
"""Error-record analysis port (§2) — read-only incident evidence.

The analysis layer (error-record-analysis-design.md §2) reads structured
watchdog incidents (L1 `watchdog_incidents` + L2 `context_jsonb`) and produces a
decision packet. It never executes fixes; it is intent-only and read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class IncidentEvidence:
    """One watchdog incident row (L1 + L2) as analysis input."""

    incident_id: int
    component: str
    status: str
    symptom: str | None
    fail_count: int
    reopen_count: int
    detected_at: str
    context: dict[str, Any] = field(default_factory=dict)


class ErrorAnalysisRepository(Protocol):
    async def list_incidents(self, since_iso: str) -> list[IncidentEvidence]: ...


@dataclass(frozen=True)
class Hypothesis:
    """One LLM-proposed root-cause hypothesis (rules-confirmed downstream)."""

    hypothesis: str
    confidence: float
    rationale: str = ""


class HypothesisPort(Protocol):
    """LLM second stage (§2.3 step 3-4); read-only, intent-only."""

    async def hypothesize(self, messages: list[dict[str, str]]) -> list[Hypothesis]: ...
