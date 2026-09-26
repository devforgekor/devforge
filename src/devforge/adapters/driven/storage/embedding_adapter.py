#!/usr/bin/env python3.12
# Status: experimental
# Path: W7 composition root (devforge.cli pipeline orchestrate)
"""PostgreSQL adapter for EmbedPort — SQL parity with scripts/pipelines/embed_batch.py."""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence
from uuid import UUID

from sqlalchemy import text

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
from devforge.core.logging import get_logger
from devforge.ports.embed import EmbedPort, PendingEmbedTurn

logger = get_logger(__name__)

MODEL = "qwen3-embedding-8b-v1"


class PostgresEmbedAdapter(EmbedPort):
    def __init__(self, gateway: DatabaseGateway) -> None:
        self._gateway = gateway

    async def cleanup_orphaned(self) -> int:
        stmt = text(
            "DELETE FROM embeddings e WHERE e.source_type = 'turn' AND e.model_name = :model"
            " AND NOT EXISTS (SELECT 1 FROM turns t WHERE t.id = e.source_id AND t.pipeline_state = 'embedded')"
        )
        async with self._gateway.session() as db:
            result = await db.execute(stmt, {"model": MODEL})
            return int(getattr(result, "rowcount", 0) or 0)

    async def dead_letter_short(self) -> int:
        stmt = text(
            "UPDATE turns SET pipeline_state = 'embed_skipped'"
            " WHERE pipeline_state = 'enriched'"
            " AND COALESCE(text_clean, text_clean_polished) IS NOT NULL"
            " AND LENGTH(TRIM(COALESCE(user_turn_clean, user_turn_clean_polished, '') || ' ' ||"
            " COALESCE(text_clean, text_clean_polished, ''))) < 15"
            " RETURNING id"
        )
        async with self._gateway.session() as db:
            result = await db.execute(stmt)
            return len(result.fetchall())

    async def count_clean_missing(self) -> int:
        stmt = text(
            "SELECT COUNT(*) FROM turns t"
            " WHERE t.pipeline_state = 'enriched'"
            " AND NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.source_type = 'turn'"
            "   AND e.source_id = t.id AND e.model_name = :model)"
            " AND COALESCE(t.text_clean, t.text_clean_polished) IS NULL"
        )
        async with self._gateway.session() as db:
            result = await db.execute(stmt, {"model": MODEL})
            return int(result.scalar() or 0)

    async def fetch_pending_turns(self, limit: int) -> list[PendingEmbedTurn]:
        stmt = text(
            "SELECT t.id, COALESCE(t.user_turn_clean, t.user_turn_clean_polished, '') AS user_text,"
            " COALESCE(t.text_clean, t.text_clean_polished) AS resp_text,"
            " t.agent, t.meta->>'model' AS model, t.created_at::text AS created_at, t.est_chars"
            " FROM turns t"
            " WHERE NOT EXISTS (SELECT 1 FROM embeddings e WHERE e.source_type = 'turn'"
            "   AND e.source_id = t.id AND e.model_name = :model)"
            " AND t.pipeline_state = 'enriched'"
            " AND COALESCE(t.text_clean, t.text_clean_polished) IS NOT NULL"
            " AND LENGTH(TRIM(COALESCE(t.user_turn_clean, t.user_turn_clean_polished, '') || ' ' ||"
            "   COALESCE(t.text_clean, t.text_clean_polished, ''))) >= 15"
            " AND (t.retry_count IS NULL OR t.retry_count < 3)"
            " ORDER BY t.est_chars ASC NULLS LAST, t.created_at DESC"
            " LIMIT :limit"
        )
        async with self._gateway.session() as db:
            result = await db.execute(stmt, {"model": MODEL, "limit": limit})
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
            "INSERT INTO embeddings (source_type, source_id, embed_text, embedding, model_name, chunk_index, metadata)"
            " VALUES ('turn', :sid, :etext, :vec::vector, :model, :ci, :meta::jsonb)"
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
        stmt = text("UPDATE turns SET pipeline_state = 'embedded' WHERE id = :sid")
        async with self._gateway.session() as db:
            await db.execute(stmt, {"sid": turn_id})

    async def bump_retry(self, turn_id: UUID) -> int:
        stmt = text(
            "UPDATE turns SET retry_count = COALESCE(retry_count, 0) + 1"
            " WHERE id = :sid RETURNING retry_count"
        )
        async with self._gateway.session() as db:
            result = await db.execute(stmt, {"sid": turn_id})
            row = result.first()
            return int(row[0]) if row else 0
