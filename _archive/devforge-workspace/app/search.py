import json
import logging
from typing import Any, Dict, List, Optional

from .db import get_pool

logger = logging.getLogger(__name__)


async def search_memories(
    query: str, source: Optional[str] = None, limit: int = 20
) -> List[Dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id AS conversation_id, c.title, c.source, c.model,
                   t.id AS turn_id, t.seq, t.user_query, t.reasoning,
                   t.assistant_answer, t.meta, t.wing, t.room, t.created_at
            FROM turns t
            JOIN conversations c ON t.conversation_id = c.id
            WHERE ($1::text IS NULL OR c.source = $1)
              AND (COALESCE(t.user_query,'') || ' ' || COALESCE(t.assistant_answer,'') || ' ' || COALESCE(t.reasoning,'')) % $2
            ORDER BY similarity(
                COALESCE(t.user_query,'') || ' ' || COALESCE(t.assistant_answer,'') || ' ' || COALESCE(t.reasoning,''),
                $2
            ) DESC
            LIMIT $3
            """,
            source,
            query,
            limit,
        )
        return [dict(r) for r in rows]


async def save_memory(
    source: str,
    user_query: str,
    assistant_answer: str,
    title: Optional[str] = None,
    model: Optional[str] = None,
    reasoning: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    wing: Optional[str] = None,
    room: Optional[str] = None,
    conversation_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Dedup by source_message_id (AI tool's original event UUID)
        if source_message_id:
            existing = await conn.fetchrow(
                "SELECT id, conversation_id, seq FROM turns WHERE source_message_id = $1",
                source_message_id,
            )
            if existing:
                return {
                    "conversation_id": str(existing["conversation_id"]),
                    "turn_id": str(existing["id"]),
                    "seq": existing["seq"],
                    "duplicate": True,
                }

        async with conn.transaction():
            cid = conversation_id
            if cid:
                exists = await conn.fetchval(
                    "SELECT 1 FROM conversations WHERE id = $1", cid
                )
                if not exists:
                    cid = None
            if not cid:
                cid = await conn.fetchval(
                    """
                    INSERT INTO conversations (title, source, model)
                    VALUES ($1, $2, $3)
                    RETURNING id
                    """,
                    title or user_query[:80],
                    source,
                    model,
                )

            seq = await conn.fetchval(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM turns WHERE conversation_id = $1",
                cid,
            )

            turn_id = await conn.fetchval(
                """
                INSERT INTO turns (conversation_id, seq, user_query, reasoning,
                                   assistant_answer, meta, wing, room, agent,
                                   source_message_id)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
                RETURNING id
                """,
                cid,
                seq,
                user_query,
                reasoning,
                assistant_answer,
                json.dumps(meta or {}, ensure_ascii=False),
                wing,
                room,
                source,
                source_message_id,
            )

    return {"conversation_id": str(cid), "turn_id": str(turn_id), "seq": seq}
