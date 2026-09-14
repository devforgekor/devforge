"""Fix initial schema drift for extract pipeline and turn search.

Revision ID: 20260914_fix_initial_schema
Revises: 20260913_initial
Create Date: 2026-09-14
"""
from __future__ import annotations

from alembic import op


revision: str = "20260914_fix_initial_schema"
down_revision: str | None = "20260913_initial"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.execute(
        "ALTER TABLE turns ADD COLUMN IF NOT EXISTS pipeline_state TEXT DEFAULT 'scanned'"
    )
    op.execute(
        "ALTER TABLE turns ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown'"
    )
    op.execute("UPDATE turns SET pipeline_state = 'scanned' WHERE pipeline_state IS NULL")
    op.execute("UPDATE turns SET source = 'unknown' WHERE source IS NULL")
    op.execute("ALTER TABLE turns ALTER COLUMN pipeline_state SET DEFAULT 'scanned'")
    op.execute("ALTER TABLE turns ALTER COLUMN source SET DEFAULT 'unknown'")
    op.execute("ALTER TABLE turns ALTER COLUMN pipeline_state SET NOT NULL")
    op.execute("ALTER TABLE turns ALTER COLUMN source SET NOT NULL")

    op.execute("DROP INDEX IF EXISTS idx_turns_search")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_turns_search "
        "ON turns USING gin "
        "((COALESCE(user_turn, '') || ' ' || COALESCE(text, '') || ' ' || COALESCE(thinking, '')) gin_trgm_ops)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_turns_pipeline_state ON turns (pipeline_state)")

    op.execute(
        "CREATE TABLE IF NOT EXISTS review_facts ("
        "id UUID DEFAULT gen_random_uuid() PRIMARY KEY, "
        "turn_id UUID NOT NULL REFERENCES turns(id) ON DELETE CASCADE, "
        "fact_index INTEGER NOT NULL, "
        "fact_type TEXT NOT NULL, "
        "evidence TEXT NOT NULL, "
        "extract_model TEXT NOT NULL, "
        "verdict TEXT DEFAULT 'passed', "
        "source TEXT DEFAULT 'unknown' NOT NULL, "
        "fact_action TEXT DEFAULT 'extracted', "
        "prompt_tokens INTEGER, "
        "gen_tokens INTEGER, "
        "elapsed_ms DOUBLE PRECISION, "
        "faithful_score DOUBLE PRECISION, "
        "faithful_method TEXT, "
        "nli_verdict TEXT, "
        "nli_llm TEXT, "
        "nli_llm2 TEXT, "
        "source_file TEXT, "
        "corrected_evidence TEXT, "
        "subject TEXT, "
        "predicate TEXT, "
        "object TEXT, "
        "qualifiers JSONB DEFAULT '{}'::jsonb, "
        "quality_checks JSONB DEFAULT '{}'::jsonb, "
        "created_at TIMESTAMPTZ DEFAULT now() NOT NULL"
        ")"
    )

    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_verdict TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm2 TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS quality_checks JSONB DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown' NOT NULL"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS fact_action TEXT DEFAULT 'extracted'"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS verdict TEXT DEFAULT 'passed'"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS source_file TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS corrected_evidence TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS subject TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS predicate TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS object TEXT"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS qualifiers JSONB DEFAULT '{}'::jsonb"
    )
    op.execute(
        "ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now() NOT NULL"
    )

    op.execute("CREATE INDEX IF NOT EXISTS idx_review_turn ON review_facts (turn_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_review_type ON review_facts (fact_type)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_review_verdict ON review_facts (verdict)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_review_source ON review_facts (source)")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_review_facts_turn_fact_model "
        "ON review_facts (turn_id, fact_index, extract_model)"
    )


def downgrade() -> None:
    pass
