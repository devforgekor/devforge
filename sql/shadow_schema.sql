-- ============================================================
-- Shadow DB — Phase 1.5 (정본: docs/adr/0003-shadow-db.md)
-- 목적: 구/신 파이프라인 대조용 분리 스키마. 프로덕션 DB와 경합하지 않는다.
-- 실행: psql "$DEVFORGE_DATABASE_URL" -f sql/shadow_schema.sql
-- 되돌리기: DROP SCHEMA devforge_shadow CASCADE;
-- ============================================================

CREATE SCHEMA IF NOT EXISTS devforge_shadow;

-- 읽기 전용 뷰: 프로덕션 turns 스냅샷 조회 (쓰기 금지)
CREATE OR REPLACE VIEW devforge_shadow.turns_shadow AS
SELECT
    id,
    conversation_id,
    seq,
    user_turn,
    thinking,
    text,
    pipeline_state,
    created_at
FROM public.turns;

-- 섀도 파이프라인 산출물 저장 (프로덕션 review_facts와 분리)
CREATE TABLE IF NOT EXISTS devforge_shadow.review_facts_shadow (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id UUID NOT NULL,
    fact_index INTEGER NOT NULL,
    fact_type TEXT NOT NULL,
    evidence TEXT NOT NULL,
    extract_model TEXT NOT NULL,
    verdict TEXT DEFAULT 'passed',
    source TEXT DEFAULT 'shadow' NOT NULL,
    nli_verdict TEXT,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT uq_shadow_review_facts UNIQUE (turn_id, fact_index, extract_model)
);

CREATE INDEX IF NOT EXISTS idx_shadow_review_turn
    ON devforge_shadow.review_facts_shadow (turn_id);

CREATE INDEX IF NOT EXISTS idx_shadow_review_model
    ON devforge_shadow.review_facts_shadow (extract_model);
