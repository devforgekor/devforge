-- Handover tables for session context capture (update_handover.py)
-- Created: 2026-06-26

CREATE TABLE IF NOT EXISTS session_checkpoints (
    id BIGINT NOT NULL PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    summary TEXT NOT NULL,
    total_files INTEGER DEFAULT 0,
    recent_files JSONB DEFAULT '{}'::jsonb,
    git_state JSONB DEFAULT '{}'::jsonb,
    task TEXT,
    content_hash TEXT,
    replaced_by BIGINT REFERENCES session_checkpoints(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id BIGINT NOT NULL PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checkpoint_id BIGINT REFERENCES session_checkpoints(id) ON DELETE SET NULL,
    decision_id TEXT,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    archived_at TIMESTAMPTZ,
    decision_text TEXT  -- legacy; may be NULL after structured migration
);

CREATE TABLE IF NOT EXISTS known_issues (
    id BIGINT NOT NULL PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checkpoint_id BIGINT REFERENCES session_checkpoints(id) ON DELETE SET NULL,
    issue_id TEXT,
    detail TEXT,
    issue_text TEXT,  -- legacy; may be NULL after structured migration
    resolved BOOLEAN NOT NULL DEFAULT false,
    resolved_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS completed_log (
    id BIGINT NOT NULL PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    checkpoint_id BIGINT REFERENCES session_checkpoints(id) ON DELETE SET NULL,
    log_text TEXT NOT NULL
);
