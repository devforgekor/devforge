"""turns provenance: confidence + updated_at (standard §3.4)

Additive only (safe): new columns default to 1.0 / now(), so legacy writers
(turn_watcher, mcp ingest, extract) keep working unchanged. The `updated_at`
trigger lives here so every existing UPDATE path (pipeline_state churn included)
keeps it fresh without touching ~10 call sites.

Revision ID: 20260929_turn_provenance
Revises: 20260923_error_record
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_turn_provenance"
down_revision: str | None = "20260923_error_record"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "turns",
        sa.Column(
            "confidence",
            sa.REAL(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
    )
    op.add_column(
        "turns",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    # [WHY] ADD COLUMN이 전체 행을 now()로 채우는데, 최초 상태에서
    # 유일한 변경은 생성 시점이므로 created_at이 맞다.
    op.execute("UPDATE turns SET updated_at = created_at")

    # [WHY] legacy 백필분은 수집 경로를 개별 검증하지 않았다 → provenance 신뢰도 하향.
    op.execute(
        "UPDATE turns SET confidence = 0.5 WHERE source = 'legacy:pre-2026-09'"
    )

    # [WHY] updated_at을 유지할 UPDATE 경로가 10곳이 넘고 앞으로도 늘어난다.
    # 한 곳이라 놓치면 컬럼 전체가 장식이 되므로 DB 트리거로 단일화한다.
    op.execute(
        "CREATE OR REPLACE FUNCTION turns_touch_updated_at() RETURNS trigger AS "
        "$fn$ BEGIN NEW.updated_at = now(); RETURN NEW; END; $fn$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_turns_updated_at BEFORE UPDATE ON turns "
        "FOR EACH ROW EXECUTE FUNCTION turns_touch_updated_at()"
    )

    op.execute(
        "COMMENT ON COLUMN turns.confidence IS "
        "'Provenance trust 0..1 for the capture path. 1.0 = verified collector, "
        "0.5 = legacy backfill (source era marker only).' "
    )
    op.execute(
        "COMMENT ON COLUMN turns.updated_at IS "
        "'Last row mutation. Maintained by trigger trg_turns_updated_at.' "
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_turns_updated_at ON turns")
    op.execute("DROP FUNCTION IF EXISTS turns_touch_updated_at()")
    op.drop_column("turns", "updated_at")
    op.drop_column("turns", "confidence")
