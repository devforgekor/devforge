import os
import logging
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

_pool: Optional[asyncpg.Pool] = None

SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    source TEXT NOT NULL,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS turns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INT NOT NULL,
    user_turn TEXT NOT NULL,
    thinking TEXT,
    text TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}',
    wing TEXT,
    room TEXT,
    agent TEXT,
    source_message_id TEXT,
    embedding vector(768),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(conversation_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_source_msg ON turns(source_message_id) WHERE source_message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS obs_dec (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id UUID NOT NULL UNIQUE REFERENCES turns(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (length(trim(decision)) > 0),
    rationale TEXT,
    context TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcp_dec (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    summary TEXT,
    detail TEXT,
    turn_ids UUID[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'qwen_worker',
    context JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_category ON observations(category);

CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source);
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at DESC);
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'turns_conversation_seq_unique'
    ) THEN
        ALTER TABLE turns ADD CONSTRAINT turns_conversation_seq_unique UNIQUE(conversation_id, seq);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_turns_search ON turns USING GIN (
    (COALESCE(user_turn, '') || ' ' ||
     COALESCE(text, '') || ' ' ||
     COALESCE(thinking, '')) gin_trgm_ops
);

CREATE INDEX IF NOT EXISTS idx_turns_meta_type ON turns ((meta->>'type'));

CREATE INDEX IF NOT EXISTS idx_obs_dec_created ON obs_dec(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_dec_decision ON obs_dec USING GIN (decision gin_trgm_ops);
"""


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        url = os.getenv("DEVFORGE_DATABASE_URL")
        if not url:
            url = "postgresql://devforge:devforge_app_2026@data-pod:5432/devforge_app"
        _pool = await asyncpg.create_pool(dsn=url, min_size=1, max_size=5, max_queries=50000)
        logger.info("DB pool created")
    return _pool


async def init_db() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(SCHEMA_SQL)
        except asyncpg.exceptions.InsufficientPrivilegeError:
            logger.info("Schema already exists, skipping init")
        else:
            logger.info("DB schema initialized")


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("DB pool closed")
