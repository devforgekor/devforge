#!/usr/bin/env python3
# Status: experimental
# Path: ports/heartbeat.py
"""Heartbeat repository port (legacy messenger.py:155-186)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class HeartbeatReading:
    """One `heartbeat_<worker>` row from watchdog_pulses."""
    worker: str      # pulse_id without the "heartbeat_" prefix
    status: str      # IN_PROGRESS | RESOLVED | IGNORED
    age_sec: int     # seconds since created_at


class HeartbeatRepository(Protocol):
    async def list_heartbeats(self) -> list[HeartbeatReading]: ...
