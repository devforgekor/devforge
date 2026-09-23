#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/storage/
"""Tests for PostgresIncidentRepository (E1) — fake gateway, no live DB."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from devforge.adapters.driven.storage.incident_pg import PostgresIncidentRepository
from devforge.ports.types import Incident


def _row(**overrides: Any) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    base = dict(
        id=1, dedup_key="svc:x:down", component="svc:x", status="open",
        symptom="down", context=None, detected_at=now, last_seen_at=now,
        action=None, action_result=None, action_at=None, resolved_at=None,
        fail_count=1, reopen_count=0, context_jsonb=None, action_error=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, *, scalar_one_or_none: Any = None, scalar_one: Any = None,
                 rows: list | None = None) -> None:
        self._soon = scalar_one_or_none
        self._one = scalar_one
        self._rows = rows or []

    def scalar_one_or_none(self) -> Any:
        return self._soon

    def scalar_one(self) -> Any:
        return self._one

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list:
        return self._rows


class _Session:
    def __init__(self, results: list[_Result]) -> None:
        self._results = list(results)
        self.executed: list[Any] = []

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.executed.append(stmt)
        return self._results.pop(0) if self._results else _Result()


class _SessionCM:
    def __init__(self, session: _Session) -> None:
        self._session = session

    async def __aenter__(self) -> _Session:
        return self._session

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Gateway:
    def __init__(self, session: _Session) -> None:
        self._session = session

    def session(self) -> _SessionCM:
        return _SessionCM(self._session)


class TestPostgresIncidentRepository:
    @pytest.mark.asyncio
    async def test_record_detect_returns_open_incident_id(self) -> None:
        session = _Session([_Result(scalar_one_or_none=_row(id=7))])
        repo = PostgresIncidentRepository(_Gateway(session))
        inc_id = await repo.record_detect("svc:x", "down", "inactive")
        assert inc_id == 7
        assert len(session.executed) == 2  # select + update (bump fail_count)

    @pytest.mark.asyncio
    async def test_record_detect_creates_new_when_none(self) -> None:
        session = _Session([
            _Result(scalar_one_or_none=None),   # no open row
            _Result(scalar_one_or_none=None),   # no recent resolved row
            _Result(scalar_one=42),             # insert ... returning id
        ])
        repo = PostgresIncidentRepository(_Gateway(session))
        inc_id = await repo.record_detect("svc:x", "down", "inactive")
        assert inc_id == 42

    @pytest.mark.asyncio
    async def test_record_detect_reopens_recent(self) -> None:
        session = _Session([
            _Result(scalar_one_or_none=None),          # no open row
            _Result(scalar_one_or_none=_row(id=9, status="resolved")),  # recent resolved
        ])
        repo = PostgresIncidentRepository(_Gateway(session))
        inc_id = await repo.record_detect("svc:x", "down", "inactive")
        assert inc_id == 9
        assert len(session.executed) == 3  # select open + select recent + update (reopen)

    @pytest.mark.asyncio
    async def test_record_action_noop_when_none(self) -> None:
        session = _Session([])
        repo = PostgresIncidentRepository(_Gateway(session))
        await repo.record_action(None, "restart", True)
        assert session.executed == []

    @pytest.mark.asyncio
    async def test_record_action_success_resolves(self) -> None:
        session = _Session([_Result()])
        repo = PostgresIncidentRepository(_Gateway(session))
        await repo.record_action(5, "restart", True)
        assert len(session.executed) == 1

    @pytest.mark.asyncio
    async def test_resolve_if_open(self) -> None:
        session = _Session([_Result()])
        repo = PostgresIncidentRepository(_Gateway(session))
        await repo.resolve_if_open("svc:x")
        assert len(session.executed) == 1

    @pytest.mark.asyncio
    async def test_find_open_maps_rows(self) -> None:
        session = _Session([_Result(rows=[_row(id=1), _row(id=2)])])
        repo = PostgresIncidentRepository(_Gateway(session))
        found = await repo.find_open("svc:x")
        assert [i.id for i in found] == [1, 2]
        assert all(isinstance(i, Incident) for i in found)

    @pytest.mark.asyncio
    async def test_record_action_records_error(self) -> None:
        session = _Session([_Result()])
        repo = PostgresIncidentRepository(_Gateway(session))
        await repo.record_action(5, "restart", False, error={"reason": "exit 1"})
        assert len(session.executed) == 1
        # action_error is carried in the UPDATE values
        assert "action_error" in session.executed[0].compile().params
