-- ============================================================
-- DevForge DB Schema v1.0 (Phase 1 — MCP 수집 + 기본 검색)
-- 적용 대상: devforge_app (PostgreSQL 16)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

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

-- ============================================================
-- 3. 관찰 기반 결정 (obs_dec)
-- ============================================================
CREATE TABLE IF NOT EXISTS obs_dec (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id UUID NOT NULL UNIQUE REFERENCES turns(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (length(trim(decision)) > 0),
    rationale TEXT,
    context TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 3a. MCP 기반 결정 (mcp_dec)
CREATE TABLE IF NOT EXISTS mcp_dec (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    summary TEXT,
    detail TEXT,
    turn_ids UUID[] DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. 관찰 기록 (Qwen worker observations)
-- ============================================================
CREATE TABLE IF NOT EXISTS observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    observation TEXT NOT NULL CHECK (length(trim(observation)) > 0),
    category TEXT NOT NULL DEFAULT 'general',
    source TEXT NOT NULL DEFAULT 'qwen_worker',
    context JSONB DEFAULT '{}',
    tags JSONB DEFAULT '{}',             -- {"tier": ["deep-dive"], "domain": ["mcp","search"]}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_observations_created ON observations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_category ON observations(category);
CREATE INDEX IF NOT EXISTS idx_observations_tags ON observations USING GIN(tags jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_observations_trgm ON observations USING GIN(observation gin_trgm_ops);

-- ============================================================
-- 5. 인덱스
-- ============================================================
CREATE INDEX IF NOT EXISTS idx_conversations_source ON conversations(source);
CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_at DESC);

-- 통합 검색 인덱스 (pg_trgm — ILIKE '%keyword%' 가속)
CREATE INDEX IF NOT EXISTS idx_turns_search ON turns USING GIN (
    (COALESCE(user_turn, '') || ' ' ||
     COALESCE(text, '') || ' ' ||
     COALESCE(thinking, '')) gin_trgm_ops
);

-- meta.type 태그 검색 (decision, error, code 등 필터링)
CREATE INDEX IF NOT EXISTS idx_turns_meta_type ON turns ((meta->>'type'));

CREATE INDEX IF NOT EXISTS idx_obs_dec_created ON obs_dec(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_obs_dec_decision ON obs_dec USING GIN (decision gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_mcp_dec_created ON mcp_dec(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcp_dec_summary ON mcp_dec USING GIN (summary gin_trgm_ops);

-- ============================================================
-- 10. Embeddings (vector search)
-- ============================================================
CREATE TABLE IF NOT EXISTS embeddings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type     TEXT NOT NULL,           -- 'turn' | 'review_fact' | 'feedback_example'
    source_id       UUID NOT NULL,
    embed_text      TEXT NOT NULL,
    embedding       vector(2048),
    model_name      TEXT NOT NULL DEFAULT 'qwen3-embedding-8b-v1',
    chunk_index     INTEGER NOT NULL DEFAULT 0,  -- 0-based chunk index for long turns
    metadata        JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_embeddings_unique
    ON embeddings (source_type, source_id, model_name, chunk_index);
CREATE INDEX IF NOT EXISTS idx_embeddings_source
    ON embeddings (source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_source_chunk
    ON embeddings (source_type, source_id, chunk_index);

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

-- 임베딩 벡터 (pgvector)
ALTER TABLE turns ADD COLUMN embedding vector(768);
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

-- ============================================================
-- 운영: activity_log (통합 이벤트 기록 — v1.3)
-- ============================================================
CREATE TABLE IF NOT EXISTS activity_log (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    type            TEXT NOT NULL,
    source          TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    agent           TEXT,
    model           TEXT,
    body            JSONB DEFAULT '{}',
    tags            TEXT[] DEFAULT '{}',
    git_commit_hash TEXT,
    run_id          TEXT,
    trace_id        TEXT,
    parent_id       BIGINT,
    turn_ids        UUID[] DEFAULT '{}',
    summary_status  TEXT NOT NULL DEFAULT 'raw',
    queue_status    TEXT NOT NULL DEFAULT 'unprocessed',
    exec_status     TEXT NOT NULL DEFAULT 'DONE'
);

CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activity_type ON activity_log(type);
CREATE INDEX IF NOT EXISTS idx_activity_source ON activity_log(source);
CREATE INDEX IF NOT EXISTS idx_activity_tags ON activity_log USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_activity_body_gin ON activity_log USING GIN(body);
CREATE INDEX IF NOT EXISTS idx_activity_commit ON activity_log(git_commit_hash)
    WHERE git_commit_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_run ON activity_log(run_id)
    WHERE run_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_trace ON activity_log(trace_id)
    WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_parent ON activity_log(parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_activity_queue ON activity_log(queue_status, created_at)
    WHERE queue_status = 'unprocessed';

CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_commit_unique
    ON activity_log(git_commit_hash)
    WHERE git_commit_hash IS NOT NULL AND type = 'commit';
CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_stage_unique
    ON activity_log(run_id, type, parent_id)
    WHERE run_id IS NOT NULL AND type = 'stage' AND exec_status = 'DONE';

-- ============================================================
-- 11. File Registry (added 2026-06-07)
-- ============================================================
CREATE TABLE IF NOT EXISTS file_registry (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        TEXT NOT NULL,
    path            TEXT NOT NULL,              -- /opt/ai_data/uploads/YYYY-MM-DD/UUID.ext
    size            BIGINT,
    hash            TEXT,                       -- SHA256 (중복 방지)
    mime_type       TEXT,
    source          TEXT NOT NULL,              -- telegram_upload | pipeline_output | agent_generate
    description     TEXT,                       -- LLM-generated summary (for search)
    tags            TEXT[] DEFAULT '{}',
    turn_id         UUID,                       -- associated turn (optional FK to turns)
    blob_url        TEXT,                       -- Azure Blob SAS URL
    sender          TEXT,                       -- Telegram user ID etc.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_file_registry_tags ON file_registry USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_file_registry_source ON file_registry (source);
CREATE INDEX IF NOT EXISTS idx_file_registry_desc_trgm ON file_registry USING GIN (description gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_file_registry_filename_trgm ON file_registry USING GIN (filename gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_file_registry_created ON file_registry (created_at DESC);

-- ============================================================
-- 12. Reflex Rules (Pattern 2+4 — auto-fix rule lifecycle)
-- Added 2026-06-28
-- ============================================================
-- Single-DB approach: reflex_rules shares PostgreSQL with observations.
-- Rule lifecycle: candidate → approved → dormant → archived
-- Pattern mining via SQL window functions (no external service needed).
-- ============================================================
CREATE TABLE IF NOT EXISTS reflex_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_category TEXT,               -- observations.category match (NULL = any)
    trigger_source   TEXT,               -- observations.source match (NULL = any)
    trigger_tags     JSONB DEFAULT '{}', -- observations tags containment match
    trigger_pattern  TEXT,               -- observation ILIKE pattern
    trigger_min_count INT DEFAULT 1,     -- minimum occurrences to trigger
    trigger_window_hours INT DEFAULT 24, -- time window for count check

    action_type     TEXT NOT NULL,       -- 'auto_fix' | 'notify' | 'escalate'
    action_params   JSONB DEFAULT '{}',  -- {"function": "raise_timeout", "args": {"mcp": "..."}}

    confidence      REAL DEFAULT 0.0,    -- 0.0 ~ 1.0
    status          TEXT NOT NULL DEFAULT 'candidate',  -- candidate | approved | dormant | archived

    description     TEXT,                -- human-readable rule explanation
    rationale       TEXT,                -- why this rule exists (source observation insight)

    observation_count INT DEFAULT 0,     -- matched observation count
    last_matched_at TIMESTAMPTZ,
    last_applied_at TIMESTAMPTZ,

    supersedes      UUID REFERENCES reflex_rules(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT reflex_rules_status_check CHECK (status IN ('candidate', 'approved', 'dormant', 'archived')),
    CONSTRAINT reflex_rules_action_check CHECK (action_type IN ('auto_fix', 'notify', 'escalate')),
    CONSTRAINT reflex_rules_confidence_check CHECK (confidence >= 0.0 AND confidence <= 1.0)
);
CREATE INDEX IF NOT EXISTS idx_reflex_rules_status ON reflex_rules(status);
CREATE INDEX IF NOT EXISTS idx_reflex_rules_trigger ON reflex_rules(trigger_category, trigger_source);
CREATE INDEX IF NOT EXISTS idx_reflex_rules_pattern ON reflex_rules USING GIN(trigger_pattern gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_reflex_rules_tags ON reflex_rules USING GIN(trigger_tags jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_reflex_rules_updated ON reflex_rules(updated_at DESC);

COMMENT ON TABLE reflex_rules IS 'Pattern 2+4 auto-fix rules — mined from observations, lifecycle-managed';
COMMENT ON COLUMN reflex_rules.status IS 'candidate:pattern found|approved:user confirmed|dormant:30d no match|archived:explicit archive';
COMMENT ON COLUMN reflex_rules.confidence IS 'Statistical confidence based on observation match frequency';
COMMENT ON COLUMN reflex_rules.supersedes IS 'Previous rule ID that this rule replaces (for contradiction resolution)';

-- ============================================================
-- 15. Deep Dive 세션 단계 heartbeat (Phase 1 — hang 감지)
-- ============================================================
-- 대화형 Deep Dive 세션(Copilot CLI 등)의 단계별 진행 상황을 기록.
-- devforge-mcp가 deepdive_step_enter/exit/session_heartbeat 툴로 관리.
-- elapsed_sec는 2주 축적 후 Phase 2 percentile 재교정에 사용.
CREATE TABLE IF NOT EXISTS deepdive_steps (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      TEXT NOT NULL,
    step            INT NOT NULL CHECK (step >= 1 AND step <= 7),
    step_name       TEXT NOT NULL,
    base_timeout_sec INT NOT NULL,
    min_bound_sec   INT NOT NULL,
    max_bound_sec   INT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at        TIMESTAMPTZ,
    elapsed_sec     INT,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    overrun_count   INT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(session_id, step),
    CONSTRAINT deepdive_steps_status_check CHECK (status IN ('ACTIVE', 'DONE', 'ABORTED'))
);
CREATE INDEX IF NOT EXISTS idx_deepdive_steps_status ON deepdive_steps(status, started_at);
CREATE INDEX IF NOT EXISTS idx_deepdive_steps_session ON deepdive_steps(session_id);

COMMENT ON TABLE deepdive_steps IS 'Deep Dive 단계 heartbeat — 단계별 진행/만료 추적, 실측 소요시간 기록';
COMMENT ON COLUMN deepdive_steps.status IS 'ACTIVE:진행중|DONE:정상종료|ABORTED:3회 초과로 자동중단';
COMMENT ON COLUMN deepdive_steps.overrun_count IS 'max_bound 초과 횟수 — 1·2회 경고, 3회 자동 ABORTED';
