#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/storage/
"""Atomic JSON persistence, legacy-compatible (state.py:290-336)."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, cast

from devforge.ports.state_persistence import StateStoragePort

log = logging.getLogger(__name__)
DEFAULT_STATE_FILE = "/opt/ai_data/scripts/watchdog_state.json"


class JsonStateStorage(StateStoragePort):
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = Path(path or os.environ.get("WATCHDOG_STATE_FILE", DEFAULT_STATE_FILE))

    def save(self, trackers: Mapping[str, Any], last_heartbeat_ts: float, mode: str) -> bool:
        try:
            payload = {  # exact legacy shape (state.py:300-304)
                "components": [t.to_dict() for t in trackers.values()],
                "last_heartbeat_ts": last_heartbeat_ts,
                "mode": mode,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".watchdog_state_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True
            except Exception:
                os.unlink(tmp)
                raise
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to save watchdog state: %s", e)
            return False

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path) as f:
                return cast(Dict[str, Any], json.load(f))
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to load watchdog state: %s", e)
            return {}
