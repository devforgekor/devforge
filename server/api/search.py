import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import get_pool

logger = logging.getLogger(__name__)


def _parse_ts(s: str) -> datetime:
    """Parse an ISO 8601 string into a timezone-aware datetime."""
    s = s.strip().replace(" ", "+", 1) if s.count(" ") == 1 else s
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


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
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Dedup by agent + source_message_id (AI tool's original event UUID)
        if source_message_id:
            existing = await conn.fetchrow(
                "SELECT id, conversation_id, seq FROM turns WHERE agent = $1 AND source_message_id = $2",
                source,
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

            if created_at:
                turn_id = await conn.fetchval(
                    """
                    INSERT INTO turns (conversation_id, seq, user_query, reasoning,
                                       assistant_answer, meta, wing, room, agent,
                                       source_message_id, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11::timestamptz)
                    ON CONFLICT (agent, source_message_id) DO NOTHING
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
                    _parse_ts(created_at),
                )
            else:
                turn_id = await conn.fetchval(
                    """
                    INSERT INTO turns (conversation_id, seq, user_query, reasoning,
                                       assistant_answer, meta, wing, room, agent,
                                       source_message_id)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
                    ON CONFLICT (agent, source_message_id) DO NOTHING
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

            if turn_id and (meta or {}).get("type") == "decision":
                await conn.execute(
                    """
                    INSERT INTO decisions (turn_id, decision, rationale, context)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (turn_id) DO NOTHING
                    """,
                    turn_id,
                    assistant_answer,
                    reasoning,
                    user_query,
                )

        if turn_id is None:
            # Race condition: duplicate inserted between dedup check and INSERT
            existing = await conn.fetchrow(
                "SELECT id, conversation_id, seq FROM turns WHERE agent = $1 AND source_message_id = $2",
                source,
                source_message_id,
            )
            if existing:
                return {
                    "conversation_id": str(existing["conversation_id"]),
                    "turn_id": str(existing["id"]),
                    "seq": existing["seq"],
                    "duplicate": True,
                }

    return {"conversation_id": str(cid), "turn_id": str(turn_id), "seq": seq}
