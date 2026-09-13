"""Initial schema — mirrors docs/specs/schema.sql

Revision ID: 20260913_initial
Revises: 
Create Date: 2026-09-13

Generated FROM src/devforge/domain/models.py via `alembic revision --autogenerate`.
This file documents the golden image baseline — it should NOT be re-generated
unless models.py diverges from production schema.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import MetaData, func, text
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers
revision: str = "20260913_initial"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ── Extensions ──
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ── Conversations ──
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("title", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("model", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_conversations_source", "conversations", ["source"])
    op.create_index("idx_conversations_created", "conversations", [text("created_at DESC")])

    # ── Turns ──
    op.create_table(
        "turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("user_turn", sa.Text, nullable=False),
        sa.Column("thinking", sa.Text),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default=text("'{}'")),
        sa.Column("wing", sa.Text),
        sa.Column("room", sa.Text),
        sa.Column("agent", sa.Text),
        sa.Column("source_message_id", sa.Text),
        sa.Column("embedding", sa.Text),  # VECTOR(768) — see env.py for type registration
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_turns_conversation", "turns", ["conversation_id", "seq"])
    op.create_index("idx_turns_created", "turns", [text("created_at DESC")])
    op.create_index("idx_turns_source_msg", "turns", ["source_message_id"], unique=True,
                    postgresql_where=text("source_message_id IS NOT NULL"))
    op.create_index("idx_turns_search", "turns", ["id"],  # Placeholder; GIN trgm created manually
                    postgresql_using="gin")
    op.create_index("idx_turns_meta_type", "turns", [text("(meta->>'type')")])

    # ── Observations ──
    op.create_table(
        "observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("observation", sa.Text, nullable=False),
        sa.Column("category", sa.Text, nullable=False, server_default=text("'general'")),
        sa.Column("source", sa.Text, nullable=False, server_default=text("'qwen_worker'")),
        sa.Column("context", postgresql.JSONB, server_default=text("'{}'")),
        sa.Column("tags", postgresql.JSONB, server_default=text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_observations_created", "observations", [text("created_at DESC")])
    op.create_index("idx_observations_category", "observations", ["category"])
    op.create_index("idx_observations_tags", "observations", ["tags"], postgresql_using="gin")

    # ── obs_dec ──
    op.create_table(
        "obs_dec",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("turns.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text),
        sa.Column("context", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    # ── mcp_dec ──
    op.create_table(
        "mcp_dec",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE")),
        sa.Column("summary", sa.Text),
        sa.Column("detail", sa.Text),
        sa.Column("turn_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), server_default=text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )

    # ── embeddings ──
    op.create_table(
        "embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("source_type", sa.Text, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("embed_text", sa.Text, nullable=False),
        sa.Column("embedding", sa.Text),  # VECTOR(2048)
        sa.Column("model_name", sa.Text, nullable=False, server_default=text("'qwen3-embedding-8b-v1'")),
        sa.Column("chunk_index", sa.Integer, nullable=False, server_default=text("0")),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_unique_constraint("idx_embeddings_unique", "embeddings",
                                ["source_type", "source_id", "model_name", "chunk_index"])
    op.create_index("idx_embeddings_source", "embeddings", ["source_type", "source_id"])
    op.create_index("idx_embeddings_source_chunk", "embeddings", ["source_type", "source_id", "chunk_index"])

    # ── worklog ──
    op.create_table(
        "worklog_entries",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=text("'done'")),
        sa.Column("kind", sa.Text, nullable=False, server_default=text("'task'")),
        sa.Column("details", postgresql.JSONB, server_default=text("'[]'")),
        sa.Column("files", postgresql.JSONB, server_default=text("'[]'")),
        sa.Column("tags", postgresql.ARRAY(sa.Text), server_default=text("'{}'")),
        sa.Column("agent", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("turn_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), server_default=text("'{}'")),
    )
    op.create_index("idx_worklog_date", "worklog_entries", [text("date DESC")])
    op.create_index("idx_worklog_tags", "worklog_entries", ["tags"], postgresql_using="gin")
    op.create_unique_constraint("idx_worklog_unique", "worklog_entries", ["date", "title"])

    # ── activity_log ──
    op.create_table(
        "activity_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False, server_default=text("''")),
        sa.Column("agent", sa.Text),
        sa.Column("model", sa.Text),
        sa.Column("body", postgresql.JSONB, server_default=text("'{}'")),
        sa.Column("tags", postgresql.ARRAY(sa.Text), server_default=text("'{}'")),
        sa.Column("git_commit_hash", sa.Text),
        sa.Column("run_id", sa.Text),
        sa.Column("trace_id", sa.Text),
        sa.Column("parent_id", sa.Integer),
        sa.Column("turn_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), server_default=text("'{}'")),
        sa.Column("summary_status", sa.Text, nullable=False, server_default=text("'raw'")),
        sa.Column("queue_status", sa.Text, nullable=False, server_default=text("'unprocessed'")),
        sa.Column("exec_status", sa.Text, nullable=False, server_default=text("'DONE'")),
    )
    op.create_index("idx_activity_created", "activity_log", [text("created_at DESC")])
    op.create_index("idx_activity_type", "activity_log", ["type"])
    op.create_index("idx_activity_source", "activity_log", ["source"])
    op.create_index("idx_activity_tags", "activity_log", ["tags"], postgresql_using="gin")
    op.create_index("idx_activity_body_gin", "activity_log", ["body"], postgresql_using="gin")
    op.create_index("idx_activity_commit", "activity_log", ["git_commit_hash"],
                    postgresql_where=text("git_commit_hash IS NOT NULL"))
    op.create_index("idx_activity_run", "activity_log", ["run_id"],
                    postgresql_where=text("run_id IS NOT NULL"))
    op.create_index("idx_activity_trace", "activity_log", ["trace_id"],
                    postgresql_where=text("trace_id IS NOT NULL"))
    op.create_index("idx_activity_parent", "activity_log", ["parent_id"],
                    postgresql_where=text("parent_id IS NOT NULL"))
    op.create_index("idx_activity_queue", "activity_log", ["queue_status", "created_at"],
                    postgresql_where=text("queue_status = 'unprocessed'"))

    # ── file_registry ──
    op.create_table(
        "file_registry",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("size", sa.BigInteger),
        sa.Column("hash", sa.Text),
        sa.Column("mime_type", sa.Text),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("tags", postgresql.ARRAY(sa.Text), server_default=text("'{}'")),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("turns.id")),
        sa.Column("blob_url", sa.Text),
        sa.Column("sender", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_file_registry_tags", "file_registry", ["tags"], postgresql_using="gin")
    op.create_index("idx_file_registry_source", "file_registry", ["source"])

    # ── reflex_rules ──
    op.create_table(
        "reflex_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("trigger_category", sa.Text),
        sa.Column("trigger_source", sa.Text),
        sa.Column("trigger_tags", postgresql.JSONB, server_default=text("'{}'")),
        sa.Column("trigger_pattern", sa.Text),
        sa.Column("trigger_min_count", sa.Integer, server_default=text("1")),
        sa.Column("trigger_window_hours", sa.Integer, server_default=text("24")),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("action_params", postgresql.JSONB, server_default=text("'{}'")),
        sa.Column("confidence", sa.Float, server_default=text("0.0")),
        sa.Column("status", sa.Text, nullable=False, server_default=text("'candidate'")),
        sa.Column("description", sa.Text),
        sa.Column("rationale", sa.Text),
        sa.Column("observation_count", sa.Integer, server_default=text("0")),
        sa.Column("last_matched_at", sa.DateTime(timezone=True)),
        sa.Column("last_applied_at", sa.DateTime(timezone=True)),
        sa.Column("supersedes", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("reflex_rules.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_reflex_rules_status", "reflex_rules", ["status"])
    op.create_index("idx_reflex_rules_trigger", "reflex_rules", ["trigger_category", "trigger_source"])
    op.create_index("idx_reflex_rules_tags", "reflex_rules", ["trigger_tags"], postgresql_using="gin")
    op.create_index("idx_reflex_rules_updated", "reflex_rules", [text("updated_at DESC")])

    # ── deepdive_steps ──
    op.create_table(
        "deepdive_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
        sa.Column("session_id", sa.Text, nullable=False),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("step_name", sa.Text, nullable=False),
        sa.Column("base_timeout_sec", sa.Integer, nullable=False),
        sa.Column("min_bound_sec", sa.Integer, nullable=False),
        sa.Column("max_bound_sec", sa.Integer, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("elapsed_sec", sa.Integer),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("overrun_count", sa.Integer, nullable=False, server_default=text("0")),
        sa.Column("status", sa.Text, nullable=False, server_default=text("'ACTIVE'")),
        sa.Column("affected_files", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.UniqueConstraint("session_id", "step", name="uq_deepdive_session_step"),
    )
    op.create_index("idx_deepdive_steps_status", "deepdive_steps", ["status", "started_at"])
    op.create_index("idx_deepdive_steps_session", "deepdive_steps", ["session_id"])

    # ── watchdog_incidents ──
    op.create_table(
        "watchdog_incidents",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("dedup_key", sa.Text, nullable=False),
        sa.Column("component", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default=text("'open'")),
        sa.Column("symptom", sa.Text),
        sa.Column("context", sa.Text),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("action", sa.Text),
        sa.Column("action_result", sa.Text),
        sa.Column("action_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("fail_count", sa.Integer, nullable=False, server_default=text("1")),
        sa.Column("reopen_count", sa.Integer, nullable=False, server_default=text("0")),
    )
    op.create_index("idx_watchdog_incidents_open", "watchdog_incidents", ["status", "dedup_key"])
    op.create_index("idx_watchdog_incidents_created", "watchdog_incidents", [text("detected_at DESC")])

    # ── golden_image_versions + deployment_logs + health_checks ──
    op.create_table(
        "golden_image_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("version", sa.Text, nullable=False, unique=True),
        sa.Column("image_id", sa.Text),
        sa.Column("status", sa.Text, nullable=False, server_default=text("'active'")),
        sa.Column("memo", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_golden_versions_status", "golden_image_versions", ["status"])
    op.create_index("idx_golden_versions_created", "golden_image_versions", [text("created_at DESC")])

    op.create_table(
        "deployment_logs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("vm_name", sa.Text, nullable=False),
        sa.Column("version", sa.Text, sa.ForeignKey("golden_image_versions.version")),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("public_ip", sa.Text),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_deployment_status", "deployment_logs", ["status"])
    op.create_index("idx_deployment_created", "deployment_logs", [text("created_at DESC")])

    op.create_table(
        "health_checks",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.Integer,
                  sa.ForeignKey("deployment_logs.id", ondelete="CASCADE")),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("latency_ms", sa.Integer),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
    op.create_index("idx_health_deployment", "health_checks", ["deployment_id"])
    op.create_index("idx_health_created", "health_checks", [text("created_at DESC")])


def downgrade() -> None:
    op.drop_table("health_checks")
    op.drop_table("deployment_logs")
    op.drop_table("golden_image_versions")
    op.drop_table("watchdog_incidents")
    op.drop_table("deepdive_steps")
    op.drop_table("reflex_rules")
    op.drop_table("file_registry")
    op.drop_table("activity_log")
    op.drop_table("worklog_entries")
    op.drop_table("embeddings")
    op.drop_table("mcp_dec")
    op.drop_table("obs_dec")
    op.drop_table("observations")
    op.drop_table("turns")
    op.drop_table("conversations")
