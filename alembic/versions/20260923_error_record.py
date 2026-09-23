"""error record: watchdog_incidents.context_jsonb + action_error + gin index

Additive only (safe): new columns default to empty/None so the legacy watchdog
and the dry-run v2 keep working. `context` (text) is kept for now; it is removed
once no reader remains (error-record-analysis-design §3).

Revision ID: 20260923_error_record
Revises: 20260914_fix_initial_schema
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_error_record"
down_revision: str | None = "20260914_fix_initial_schema"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "watchdog_incidents",
        sa.Column(
            "context_jsonb",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "watchdog_incidents",
        sa.Column("action_error", postgresql.JSONB, nullable=True),
    )
    op.create_index(
        "idx_watchdog_incidents_context",
        "watchdog_incidents",
        ["context_jsonb"],
        postgresql_using="gin",
    )
    op.execute(
        "COMMENT ON COLUMN watchdog_incidents.context_jsonb IS "
        "'Structured diagnostics (schema_version, systemd, container, journal_tail, "
        "exception, hint). AI-readable.'"
    )


def downgrade() -> None:
    op.drop_index("idx_watchdog_incidents_context", table_name="watchdog_incidents")
    op.drop_column("watchdog_incidents", "action_error")
    op.drop_column("watchdog_incidents", "context_jsonb")
