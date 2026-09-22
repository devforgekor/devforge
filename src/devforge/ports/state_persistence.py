#!/usr/bin/env python3
# Status: experimental
# Path: ports/state_persistence.py
"""Port for watchdog state persistence (legacy watchdog_state.json)."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Protocol


class StateStoragePort(Protocol):
    """Persist/restore the legacy watchdog state payload."""

    def save(self, trackers: Mapping[str, Any], last_heartbeat_ts: float, mode: str) -> bool: ...

    def load(self) -> Dict[str, Any]:
        """Return {"components": [dict, ...], "last_heartbeat_ts": float, "mode": str}."""
        ...
