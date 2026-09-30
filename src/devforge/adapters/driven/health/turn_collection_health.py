#!/usr/bin/env python3.12
# Status: experimental
# Path: adapters/driven/health/
"""Turn collection completeness check (reads the collector's checkpoint file)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from devforge.domain.turn_collection.collection import collection_health
from devforge.ports.health_check import HealthCheckPort
from devforge.ports.types import HealthCheck

DEFAULT_CHECKPOINT = Path("/opt/projects/server/collect_checkpoint.json")
COMPONENT = "turn_collection"


class TurnCollectionHealthChecker(HealthCheckPort):
    """Reports sessions the collector has parked and stopped ingesting.

    [WHY] a parked session is skipped silently — turn_watcher records the
    decision and moves on — so no service, port or process check notices that
    collection went partially blind. This reads the symptom instead.
    """

    def __init__(self, path: Path = DEFAULT_CHECKPOINT, source: str = "opencode") -> None:
        self._path = Path(path)
        self._source = source

    def _entries(self) -> Dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        entries = data.get(self._source, {})
        return entries if isinstance(entries, dict) else {}

    async def check_health(self) -> list[HealthCheck]:
        health = collection_health(self._entries())
        kinds = ",".join(health.kinds) if health.kinds else "none"
        return [
            HealthCheck(
                COMPONENT,
                health.quarantined == 0,
                f"quarantined={health.quarantined}/{health.total} kinds={kinds}",
                metric_value=health.completeness,
                threshold=1.0,
            )
        ]
