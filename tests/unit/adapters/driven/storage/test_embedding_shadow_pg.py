#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/adapters/driven/storage/
"""Tests for PostgresEmbedShadowAdapter (W8) — fake gateway, no live DB.

Guards the per-chunk completeness contract: an interrupted shadow batch leaves
a turn with only some chunks stored, and a turn-level guard would drop it from
the pending set forever (observed as 44 orphan chunks).
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from devforge.adapters.driven.storage.embedding_shadow_adapter import (
    PostgresEmbedShadowAdapter,
)


class _Result:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows or []
        self.rowcount = 0

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict]:
        return self._rows

    def scalar(self) -> Any:
        return 0


class _Session:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self._rows = rows
        self.executed: list[Any] = []
        self.params: dict | None = None

    async def execute(self, stmt: Any, params: Any = None) -> _Result:
        self.executed.append(stmt)
        self.params = params
        return _Result(self._rows)


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


def _sql_of(session: _Session) -> str:
    return str(session.executed[-1])


@pytest.mark.asyncio
async def test_should_refetch_partial_turn_when_shadow_chunk_missing() -> None:
    session = _Session()
    adapter = PostgresEmbedShadowAdapter(_Gateway(session), since="2026-09-20T00:00:00+00:00")

    await adapter.fetch_pending_turns(limit=5)

    sql = _sql_of(session)
    assert "s.chunk_index = e.chunk_index" in sql
    assert "FROM public.embeddings e" in sql
    assert "FROM devforge_shadow.embeddings_shadow s" in sql


@pytest.mark.asyncio
async def test_should_window_on_prod_embed_time_when_since_given() -> None:
    session = _Session()
    adapter = PostgresEmbedShadowAdapter(_Gateway(session), since="2026-09-20T00:00:00+00:00")

    await adapter.fetch_pending_turns(limit=5)

    assert "CAST(:since AS timestamptz)" in _sql_of(session)
    assert session.params is not None
    assert "since" in session.params


@pytest.mark.asyncio
async def test_should_query_without_window_when_since_omitted() -> None:
    session = _Session()
    adapter = PostgresEmbedShadowAdapter(_Gateway(session))

    await adapter.fetch_pending_turns(limit=5)

    assert ":since" not in _sql_of(session)
    assert session.params is not None
    assert "since" not in session.params


@pytest.mark.asyncio
async def test_should_map_rows_to_pending_turns_when_fetch_succeeds() -> None:
    turn_id = uuid4()
    session = _Session(
        rows=[
            {
                "id": turn_id,
                "user_text": "user",
                "resp_text": "resp",
                "agent": "main",
                "model": "m",
                "created_at": "2026-09-21T00:00:00",
                "est_chars": 12,
            }
        ]
    )
    adapter = PostgresEmbedShadowAdapter(_Gateway(session))

    rows = await adapter.fetch_pending_turns(limit=1)

    assert len(rows) == 1
    assert rows[0].id == turn_id
    assert rows[0].created_at == "2026-09-21T00:00:00"


@pytest.mark.asyncio
async def test_should_never_touch_prod_rows_when_shadowing() -> None:
    session = _Session()
    adapter = PostgresEmbedShadowAdapter(_Gateway(session))

    await adapter.fetch_pending_turns(limit=5)
    await adapter.mark_embedded(uuid4())
    await adapter.bump_retry(uuid4())
    await adapter.dead_letter_short()

    sql = _sql_of(session)
    assert "UPDATE turns" not in sql
    assert "UPDATE public.turns" not in sql
    assert "INSERT INTO turns" not in sql


@pytest.mark.asyncio
async def test_should_write_only_to_shadow_table_when_storing() -> None:
    session = _Session()
    adapter = PostgresEmbedShadowAdapter(_Gateway(session))

    await adapter.store_turn_embedding(uuid4(), [0.5] * 8, "text", chunk_index=3)

    assert "INSERT INTO devforge_shadow.embeddings_shadow" in _sql_of(session)
    assert session.params is not None
    assert session.params["ci"] == 3
