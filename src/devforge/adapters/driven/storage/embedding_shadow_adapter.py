#!/usr/bin/env python3.12
# Status: experimental
# Path: devforge.cli (pipeline orchestrate --shadow)
"""Shadow EmbedPort — reprojection of prod-embedded turns into devforge_shadow (W8).

Reads prod, writes only devforge_shadow.embeddings_shadow. Prod turns are never
mutated (no state transitions, no retry bumps, no dead-letters). Sentinel rows
(retry_count >= 3) and legacy raw-text rows (clean columns NULL) are prod
failure/history artifacts and are excluded from reprojection — counted instead
by scripts/shadow_diff.py --mode embed as intentional classes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import text

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.core.logging import get_logger
from devforge.ports.embed import EmbedPort, PendingEmbedTurn

logger = get_logger(__name__)

MODEL = "qwen3-embedding-8b-v1"
SHADOW_TABLE = "devforge_shadow.embeddings_shadow"


class PostgresEmbedShadowAdapter(EmbedPort):
    def __init__(self, gateway: DatabaseGateway, since: Optional[str] = None) -> None:
        self._gateway = gateway
        # [WHY] Prod rows embedded before the shadow window may predate source
        # rewrites (clean/polish merge) and can never be reproduced — windowing
        # on prod embed time keeps the diff gate meaningful (documented class).
        # asyncpg requires a datetime (not a str) for timestamptz parameters.
        self._since: Optional[datetime] = None
        if since:
            parsed = datetime.fromisoformat(since)
            self._since = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def cleanup_orphaned(self) -> int:
        """Drop shadow rows without an in-window prod counterpart (self-heal)."""
        since_clause = " AND e.created_at >= CAST(:since AS timestamptz)" if self._since else ""
        stmt = text(
            f"DELETE FROM {SHADOW_TABLE} s WHERE s.source_type = 'turn' AND s.model_name = :model"
            " AND NOT EXISTS (SELECT 1 FROM public.embeddings e WHERE e.source_type = 'turn'"
            "   AND e.source_id = s.source_id AND e.model_name = s.model_name"
            "   AND e.chunk_index = s.chunk_index" + since_clause + ")"
        )
        params: dict[str, Any] = {"model": MODEL}
        if self._since:
            params["since"] = self._since
        async with self._gateway.session() as db:
            result = await db.execute(stmt, params)
            return int(getattr(result, "rowcount", 0) or 0)

    async def dead_letter_short(self) -> int:
        # [WHY] dead-letter writes pipeline_state on prod turns — forbidden in
        # shadow mode. Prod embed_batch owns that transition.
        return 0

    async def count_clean_missing(self) -> int:
        """Prod-embedded turns whose clean columns are NULL (legacy raw-text class)."""
        stmt = text(
            "SELECT COUNT(DISTINCT t.id) FROM turns t"
            " WHERE t.pipeline_state = 'embedded'"
            " AND EXISTS (SELECT 1 FROM public.embeddings e WHERE e.source_type = 'turn'"
            "   AND e.source_id = t.id AND e.model_name = :model)"
            " AND COALESCE(t.text_clean, t.text_clean_polished) IS NULL"
        )
        async with self._gateway.session() as db:
            result = await db.execute(stmt, {"model": MODEL})
            return int(result.scalar() or 0)

    async def fetch_pending_turns(self, limit: int) -> list[PendingEmbedTurn]:
        since_clause = " AND e.created_at >= CAST(:since AS timestamptz)" if self._since else ""
        stmt = text(
            "SELECT t.id, COALESCE(t.user_turn_clean, t.user_turn_clean_polished, '') AS user_text,"
            " COALESCE(t.text_clean, t.text_clean_polished) AS resp_text,"
            " t.agent, t.meta->>'model' AS model, t.created_at::text AS created_at, t.est_chars"
            " FROM turns t"
            " WHERE t.pipeline_state = 'embedded'"
            " AND (t.retry_count IS NULL OR t.retry_count < 3)"
            " AND COALESCE(t.text_clean, t.text_clean_polished) IS NOT NULL"
            # [WHY] Per-chunk completeness, not per-turn: an interrupted shadow
            # batch can leave a turn with some chunks stored. A turn-level
            # NOT EXISTS would skip it forever (observed 44 orphan chunks).
            " AND EXISTS (SELECT 1 FROM public.embeddings e WHERE e.source_type = 'turn'"
            "   AND e.source_id = t.id AND e.model_name = :model"
            + since_clause
            + " AND NOT EXISTS (SELECT 1 FROM devforge_shadow.embeddings_shadow s"
            "   WHERE s.source_type = 'turn' AND s.source_id = t.id"
            "     AND s.chunk_index = e.chunk_index AND s.model_name = :model))"
            " ORDER BY t.created_at DESC"
            " LIMIT :limit"
        )
        params: dict[str, Any] = {"model": MODEL, "limit": limit}
        if self._since:
            params["since"] = self._since
        async with self._gateway.session() as db:
            result = await db.execute(stmt, params)
            rows = result.mappings().all()
        return [
            PendingEmbedTurn(
                id=row["id"],
                user_text=row["user_text"],
                resp_text=row["resp_text"],
                agent=row["agent"],
                model=row["model"],
                created_at=row["created_at"] or "",
                est_chars=row["est_chars"],
            )
            for row in rows
        ]

    async def store_turn_embedding(
        self,
        turn_id: UUID,
        vector: Sequence[float],
        embed_text: str,
        chunk_index: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        vec_str = "[" + ",".join(f"{v:.8f}" for v in vector) + "]"
        stmt = text(
            f"INSERT INTO {SHADOW_TABLE} (source_type, source_id, embed_text, embedding, model_name,"
            " chunk_index, metadata) VALUES ('turn', :sid, :etext, CAST(:vec AS vector), :model, :ci,"
            " CAST(:meta AS jsonb))"
            " ON CONFLICT (source_type, source_id, model_name, chunk_index)"
            " DO UPDATE SET embedding = EXCLUDED.embedding, embed_text = EXCLUDED.embed_text,"
            " metadata = EXCLUDED.metadata, created_at = now()"
        )
        params = {
            "sid": turn_id,
            "etext": embed_text[:8000],
            "vec": vec_str,
            "model": MODEL,
            "ci": chunk_index,
            "meta": json.dumps(metadata or {}),
        }
        async with self._gateway.session() as db:
            await db.execute(stmt, params)

    async def mark_embedded(self, turn_id: UUID) -> None:
        # [WHY] pipeline_state is prod-owned — shadow never advances turn state.
        return None

    async def bump_retry(self, turn_id: UUID) -> int:
        # [WHY] retry_count is prod-owned — a failed shadow batch simply leaves
        # the turn shadow-missing and is retried next cycle (visible in diff).
        return 0
