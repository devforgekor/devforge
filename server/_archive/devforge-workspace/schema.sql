-- ============================================================
-- DevForge DB Schema v1.0 (Phase 1 — MCP 수집 + 기본 검색)
-- 적용 대상: devforge_app (PostgreSQL 16)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- 1. 대화 세션
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    source TEXT NOT NULL,
    model TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2. 개별 질문-답변 (턴)
-- ============================================================
CREATE TABLE IF NOT EXISTS turns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq INT NOT NULL,
    user_query TEXT NOT NULL,
    reasoning TEXT,
    assistant_answer TEXT NOT NULL,
    meta JSONB NOT NULL DEFAULT '{}',
    wing TEXT,
    room TEXT,
    agent TEXT,
    source_message_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(conversation_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turns_source_msg ON turns(source_message_id) WHERE source_message_id IS NOT NULL;

-- ============================================================
-- 3. 사용자 명시적 결정
-- ============================================================
CREATE TABLE IF NOT EXISTS user_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    detail TEXT,
    turn_ids UUID[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 4. 인덱스
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source);
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at DESC);

-- 통합 검색 인덱스 (pg_trgm — ILIKE '%keyword%' 가속)
CREATE INDEX IF NOT EXISTS idx_turns_search ON turns USING GIN (
    (COALESCE(user_query, '') || ' ' ||
     COALESCE(assistant_answer, '') || ' ' ||
     COALESCE(reasoning, '')) gin_trgm_ops
);

-- meta.type 태그 검색 (decision, error, code 등 필터링)
CREATE INDEX IF NOT EXISTS idx_turns_meta_type ON turns ((meta->>'type'));

-- user_decisions
CREATE INDEX IF NOT EXISTS idx_user_decisions_summary ON user_decisions USING GIN (summary gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_user_decisions_created ON user_decisions(created_at DESC);

-- ============================================================
-- Phase 2 예약 (주석)
-- ============================================================
/*
CREATE EXTENSION IF NOT EXISTS vector;

-- 분류 체계
CREATE TABLE wings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,
    description TEXT,
    color TEXT,
    sort_order INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE rooms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wing_id UUID REFERENCES wings(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    sort_order INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(wing_id, name)
);

-- Turn ↔ Room 분류 매핑
CREATE TABLE classifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id UUID REFERENCES turns(id) ON DELETE CASCADE,
    room_id UUID REFERENCES rooms(id) ON DELETE CASCADE,
    confidence REAL DEFAULT 1.0,
    source TEXT DEFAULT 'manual',  -- auto / manual
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(turn_id, room_id)
);

-- LLM 자동 추출 결정
CREATE TABLE decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    decision TEXT NOT NULL,
    rationale TEXT,
    confidence FLOAT DEFAULT 0.0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 임베딩 벡터 (pgvector)
ALTER TABLE turns ADD COLUMN embedding vector(1536);
*/

-- ============================================================
-- 운영: worklog entries (서버 작업 이력)
-- ============================================================
CREATE TABLE IF NOT EXISTS worklog_entries (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    date        DATE NOT NULL,
    title       TEXT NOT NULL,
    summary     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'done',       -- pending | in_progress | done
    kind        TEXT NOT NULL DEFAULT 'task',       -- task | decision | issue | phase
    details     JSONB DEFAULT '[]',
    files       JSONB DEFAULT '[]',
    tags        TEXT[] DEFAULT '{}',
    agent       TEXT,
    model       TEXT,
    turn_ids    UUID[] DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_worklog_date ON worklog_entries(date DESC);
CREATE INDEX IF NOT EXISTS idx_worklog_tags ON worklog_entries USING GIN(tags);
CREATE UNIQUE INDEX IF NOT EXISTS idx_worklog_unique ON worklog_entries(date, title);
CREATE UNIQUE INDEX IF NOT EXISTS idx_worklog_one_in_progress ON worklog_entries (status) WHERE status = 'in_progress';
