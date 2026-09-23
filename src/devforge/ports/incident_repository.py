#!/usr/bin/env python3
# Status: experimental
# Path: ports/incident_repository.py
"""Incident repository port (legacy incidents.py)."""

from __future__ import annotations

from typing import Optional, Protocol

from devforge.ports.types import Incident


class IncidentRepository(Protocol):
    async def record_detect(
        self, component: str, event_type: str, detail: str, unit: Optional[str] = None
    ) -> Optional[int]: ...
    async def record_action(self, incident_id: Optional[int], action: str, ok: bool) -> None: ...
    async def resolve_if_open(self, component: str) -> None: ...
    async def find_open(self, component: Optional[str] = None) -> list[Incident]: ...
