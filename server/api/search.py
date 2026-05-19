import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .db import get_pool

logger = logging.getLogger(__name__)


def _jsonb_object(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


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
                   t.id AS turn_id, t.seq, t.user_turn, t.thinking,
                   t.text, t.meta, t.wing, t.room, t.created_at
            FROM turns t
            JOIN conversations c ON t.conversation_id = c.id
            WHERE ($1::text IS NULL OR c.source = $1)
              AND (COALESCE(t.user_turn,'') || ' ' || COALESCE(t.text,'') || ' ' || COALESCE(t.thinking,'')) % $2
            ORDER BY similarity(
                COALESCE(t.user_turn,'') || ' ' || COALESCE(t.text,'') || ' ' || COALESCE(t.thinking,''),
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
    user_turn: str,
    text: str,
    title: Optional[str] = None,
    model: Optional[str] = None,
    thinking: Optional[str] = None,
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
                "SELECT id, conversation_id, seq, meta FROM turns WHERE agent = $1 AND source_message_id = $2",
                source,
                source_message_id,
            )
            if existing:
                # Merge meta
                incoming_meta = meta or {}
                existing_meta = _jsonb_object(existing["meta"])
                merged_meta = {**existing_meta, **incoming_meta}
                if merged_meta != existing_meta:
                    await conn.execute(
                        "UPDATE turns SET meta = $1::jsonb WHERE id = $2",
                        json.dumps(merged_meta, ensure_ascii=False),
                        existing["id"],
                    )
                # Backfill thinking / text if previously empty
                if thinking or text:
                    await conn.execute(
                        "UPDATE turns SET thinking = COALESCE(NULLIF(thinking, ''), $1),"
                        "                text = COALESCE(NULLIF(text, ''), $2)"
                        " WHERE id = $3 AND (thinking IS NULL OR thinking = '' OR text IS NULL OR text = '')",
                        thinking, text, existing["id"],
                    )
                return {
                    "conversation_id": str(existing["conversation_id"]),
                    "turn_id": str(existing["id"]),
                    "seq": existing["seq"],
                    "duplicate": True,
                }

        async with conn.transaction():
            cid = conversation_id
            if cid:
                try:
                    uuid.UUID(cid)
                except (ValueError, AttributeError):
                    cid = None
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
                    title or user_turn[:80],
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
                    INSERT INTO turns (conversation_id, seq, user_turn, thinking,
                                       text, meta, wing, room, agent,
                                       source_message_id, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11::timestamptz)
                    ON CONFLICT (agent, source_message_id) DO NOTHING
                    RETURNING id
                    """,
                    cid,
                    seq,
                    user_turn,
                    thinking,
                    text,
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
                    INSERT INTO turns (conversation_id, seq, user_turn, thinking,
                                       text, meta, wing, room, agent,
                                       source_message_id)
                    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
                    ON CONFLICT (agent, source_message_id) DO NOTHING
                    RETURNING id
                    """,
                    cid,
                    seq,
                    user_turn,
                    thinking,
                    text,
                    json.dumps(meta or {}, ensure_ascii=False),
                    wing,
                    room,
                    source,
                    source_message_id,
                )

            if turn_id and (meta or {}).get("type") == "decision":
                await conn.execute(
                    """
                    INSERT INTO obs_dec (turn_id, decision, rationale, context)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (turn_id) DO NOTHING
                    """,
                    turn_id,
                    text,
                    thinking,
                    user_turn,
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
