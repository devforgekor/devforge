#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driven/storage/*, application/watchdog_service.py
"""Port: incident persistence."""
from __future__ import annotations

from typing import Protocol

from devforge.ports.types import Incident


class IncidentRepository(Protocol):
    """Repository for incident persistence."""

    async def save(self, incident: Incident) -> Incident: ...
    async def resolve(self, incident_id: int, note: str) -> bool: ...
    async def find_open(self, component: str | None = None) -> list[Incident]: ...
