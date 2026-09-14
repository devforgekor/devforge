"""SQLAlchemy models for DevForge — mirrors docs/specs/schema.sql.

These models provide type-safe access to the production database.
They are the single source of truth for DB schema; Alembic migrations
are generated FROM these models (not hand-written SQL).

Schema version: v1.0 (Phase 1) + v1.3 extensions (activity_log, deepdive_steps, watchdog_incidents).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase


# ── pgvector type (minimal — registered with PostgreSQL for runtime) ──
# We use a simple Text column for schema generation; the actual type
# will be VECTOR(n) in PostgreSQL (requires the vector extension).
class Vector(TypeDecorator[Any]):
    """Minimal pgvector support for Alembic autogenerate."""

    impl = Text
    cache_ok = True

    def __init__(self, dimensions: int = 768, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dimensions = dimensions

    def load_dialect_impl(self, dialect: Any) -> Any:
        from sqlalchemy.dialects import postgresql

        return postgresql.TEXT()

    @staticmethod
    def __create_vector(dimensions: int) -> str:
        return f"vector({dimensions})"


metadata_obj = MetaData(schema="public")


class Base(DeclarativeBase):
    metadata = metadata_obj
    __allow_unmapped__ = True  # Allow legacy Column annotations without Mapped[]


# ── 1. Conversations ──


class Conversation(Base):
    __tablename__ = "conversations"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(Text)
    source = Column(Text, nullable=False)
    model = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── 2. Turns ──


class Turn(Base):
    __tablename__ = "turns"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    seq = Column(Integer, nullable=False)
    user_turn = Column(Text, nullable=False)
    thinking = Column(Text)
    text = Column(Text, nullable=False)
    meta_data = Column("meta", JSONB, nullable=False, server_default=sql_text("'{}'"))
    wing = Column(Text)
    room = Column(Text)
    agent = Column(Text)
    source_message_id = Column(Text)
    pipeline_state = Column(Text, nullable=False, server_default=sql_text("'scanned'"))
    source = Column(Text, server_default=sql_text("'unknown'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_turns_conversation", "conversation_id", "seq"),
        Index("idx_turns_created", sql_text("created_at DESC")),
        Index(
            "idx_turns_source_msg",
            "source_message_id",
            postgresql_where=sql_text("source_message_id IS NOT NULL"),
            unique=True,
        ),
        Index(
            "idx_turns_search",
            sql_text(
                "(COALESCE(user_turn, '') || ' ' || COALESCE(text, '') || ' ' || COALESCE(thinking, '')) gin_trgm_ops"
            ),
            postgresql_using="gin",
        ),
        Index("idx_turns_meta_type", sql_text("(meta->>'type')")),
        Index("idx_turns_pipeline_state", "pipeline_state"),
    )


# ── 3. Observations ──


class Observation(Base):
    __tablename__ = "observations"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    observation = Column(Text, nullable=False)
    category = Column(Text, nullable=False, server_default=sql_text("'general'"))
    source = Column(Text, nullable=False, server_default=sql_text("'qwen_worker'"))
    context = Column(JSONB, server_default=sql_text("'{}'"))
    tags = Column(JSONB, server_default=sql_text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_observations_created", sql_text("created_at DESC")),
        Index("idx_observations_category", "category"),
        Index("idx_observations_tags", postgresql_using="gin", postgresql_with={}),
        Index(
            "idx_observations_trgm", sql_text("observation gin_trgm_ops"), postgresql_using="gin"
        ),
    )


# ── 4. Review Facts ──


class ReviewFact(Base):
    """Extracted facts stored per turn — the SSOT for the extract pipeline.

    Mirrors the review_facts table used by extract.py / extract_verify.py.
    """

    __tablename__ = "review_facts"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    turn_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    fact_index = Column(Integer, nullable=False)
    fact_type = Column(Text, nullable=False)
    evidence = Column(Text, nullable=False)
    extract_model = Column(Text, nullable=False)
    verdict = Column(Text, server_default=sql_text("'passed'"))
    source = Column(Text, nullable=False)
    fact_action = Column(Text, server_default=sql_text("'extracted'"))
    prompt_tokens = Column(Integer)
    gen_tokens = Column(Integer)
    elapsed_ms = Column(Float)
    faithful_score = Column(Float)
    faithful_method = Column(Text)
    nli_verdict = Column(Text)
    nli_llm = Column(Text)
    nli_llm2 = Column(Text)
    source_file = Column(Text)
    corrected_evidence = Column(Text)
    subject = Column(Text)
    predicate = Column(Text)
    object_ = Column("object", Text)
    qualifiers = Column(JSONB, server_default=sql_text("'{}'"))
    quality_checks = Column(JSONB, server_default=sql_text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_review_turn", "turn_id"),
        Index("idx_review_type", "fact_type"),
        Index("idx_review_verdict", "verdict"),
        Index("idx_review_source", "source"),
        UniqueConstraint(
            "turn_id",
            "fact_index",
            "extract_model",
            name="uq_review_facts_turn_fact_model",
        ),
    )


# ── 5. ObsDec (관찰 기반 결정) ──


class ObsDec(Base):
    __tablename__ = "obs_dec"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    turn_id = Column(
        PG_UUID(as_uuid=True),
        ForeignKey("turns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    decision = Column(Text, nullable=False)
    rationale = Column(Text)
    context = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── 5. MCPDec (MCP 기반 결정) ──


class MCPDec(Base):
    __tablename__ = "mcp_dec"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE")
    )
    summary = Column(Text)
    detail = Column(Text)
    turn_ids = Column(ARRAY(PG_UUID(as_uuid=True)), server_default=sql_text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())


# ── 6. Embeddings ──


class Embedding(Base):
    __tablename__ = "embeddings"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type = Column(Text, nullable=False)
    source_id = Column(PG_UUID(as_uuid=True), nullable=False)
    embed_text = Column(Text, nullable=False)
    embedding = Column(Vector(2048))  # pgvector, 2048-dim for qwen3-embedding-8b
    model_name = Column(Text, nullable=False, server_default=sql_text("'qwen3-embedding-8b-v1'"))
    chunk_index = Column(Integer, nullable=False, server_default=sql_text("0"))
    metadata_ = Column("metadata", JSONB, nullable=False, server_default=sql_text("'{}'"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(
            "idx_embeddings_unique",
            "source_type",
            "source_id",
            "model_name",
            "chunk_index",
            unique=True,
        ),
        Index("idx_embeddings_source", "source_type", "source_id"),
        Index("idx_embeddings_source_chunk", "source_type", "source_id", "chunk_index"),
    )


# ── 7. Worklog ──


class WorklogEntry(Base):
    __tablename__ = "worklog_entries"

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    date = Column(Date, nullable=False)
    title = Column(Text, nullable=False)
    summary = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=sql_text("'done'"))
    kind = Column(Text, nullable=False, server_default=sql_text("'task'"))
    details = Column(JSONB, server_default=sql_text("'[]'"))
    files = Column(JSONB, server_default=sql_text("'[]'"))
    tags = Column(ARRAY(Text), server_default=sql_text("'{}'"))
    agent = Column(Text)
    model = Column(Text)
    turn_ids = Column(ARRAY(PG_UUID(as_uuid=True)), server_default=sql_text("'{}'"))

    __table_args__ = (
        Index("idx_worklog_date", sql_text("date DESC")),
        Index("idx_worklog_tags", postgresql_using="gin"),
        Index("idx_worklog_unique", "date", "title", unique=True),
        Index(
            "idx_worklog_one_in_progress",
            sql_text("(status)"),
            postgresql_where=sql_text("status = 'in_progress'"),
            unique=True,
        ),
    )


# ── 8. Activity Log ──


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    type = Column(Text, nullable=False)
    source = Column(Text, nullable=False)
    title = Column(Text, nullable=False)
    summary = Column(Text, nullable=False, server_default=sql_text("''"))
    agent = Column(Text)
    model = Column(Text)
    body = Column(JSONB, server_default=sql_text("'{}'"))
    tags = Column(ARRAY(Text), server_default=sql_text("'{}'"))
    git_commit_hash = Column(Text)
    run_id = Column(Text)
    trace_id = Column(Text)
    parent_id = Column(Integer)
    turn_ids = Column(ARRAY(PG_UUID(as_uuid=True)), server_default=sql_text("'{}'"))
    summary_status = Column(Text, nullable=False, server_default=sql_text("'raw'"))
    queue_status = Column(Text, nullable=False, server_default=sql_text("'unprocessed'"))
    exec_status = Column(Text, nullable=False, server_default=sql_text("'DONE'"))

    __table_args__ = (
        Index("idx_activity_created", sql_text("created_at DESC")),
        Index("idx_activity_type", "type"),
        Index("idx_activity_source", "source"),
        Index("idx_activity_tags", postgresql_using="gin"),
        Index("idx_activity_body_gin", postgresql_using="gin"),
        Index(
            "idx_activity_commit",
            "git_commit_hash",
            postgresql_where=sql_text("git_commit_hash IS NOT NULL"),
        ),
        Index("idx_activity_run", "run_id", postgresql_where=sql_text("run_id IS NOT NULL")),
        Index("idx_activity_trace", "trace_id", postgresql_where=sql_text("trace_id IS NOT NULL")),
        Index(
            "idx_activity_parent", "parent_id", postgresql_where=sql_text("parent_id IS NOT NULL")
        ),
        Index(
            "idx_activity_queue",
            "queue_status",
            sql_text("created_at"),
            postgresql_where=sql_text("queue_status = 'unprocessed'"),
        ),
        Index(
            "idx_activity_commit_unique",
            "git_commit_hash",
            postgresql_where=sql_text("git_commit_hash IS NOT NULL AND type = 'commit'"),
            unique=True,
        ),
        Index(
            "idx_activity_stage_unique",
            "run_id",
            "type",
            "parent_id",
            postgresql_where=sql_text(
                "run_id IS NOT NULL AND type = 'stage' AND exec_status = 'DONE'"
            ),
            unique=True,
        ),
    )


# ── 9. File Registry ──


class FileRegistry(Base):
    __tablename__ = "file_registry"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(Text, nullable=False)
    path = Column(Text, nullable=False)
    size = Column(Integer)
    hash = Column(Text)
    mime_type = Column(Text)
    source = Column(Text, nullable=False)
    description = Column(Text)
    tags = Column(ARRAY(Text), server_default=sql_text("'{}'"))
    turn_id = Column(PG_UUID(as_uuid=True), ForeignKey("turns.id"))
    blob_url = Column(Text)
    sender = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_file_registry_tags", postgresql_using="gin"),
        Index("idx_file_registry_source", "source"),
        Index(
            "idx_file_registry_desc_trgm",
            sql_text("description gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index(
            "idx_file_registry_filename_trgm",
            sql_text("filename gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index("idx_file_registry_created", sql_text("created_at DESC")),
    )


# ── 10. Reflex Rules ──


class ReflexRule(Base):
    __tablename__ = "reflex_rules"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trigger_category = Column(Text)
    trigger_source = Column(Text)
    trigger_tags = Column(JSONB, server_default=sql_text("'{}'"))
    trigger_pattern = Column(Text)
    trigger_min_count = Column(Integer, server_default=sql_text("1"))
    trigger_window_hours = Column(Integer, server_default=sql_text("24"))

    action_type = Column(Text, nullable=False)
    action_params = Column(JSONB, server_default=sql_text("'{}'"))

    confidence = Column(Float, server_default=sql_text("0.0"))
    status = Column(Text, nullable=False, server_default=sql_text("'candidate'"))

    description = Column(Text)
    rationale = Column(Text)

    observation_count = Column(Integer, server_default=sql_text("0"))
    last_matched_at = Column(DateTime(timezone=True))
    last_applied_at = Column(DateTime(timezone=True))

    supersedes = Column(PG_UUID(as_uuid=True), ForeignKey("reflex_rules.id"))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_reflex_rules_status", "status"),
        Index("idx_reflex_rules_trigger", "trigger_category", "trigger_source"),
        Index(
            "idx_reflex_rules_pattern",
            sql_text("trigger_pattern gin_trgm_ops"),
            postgresql_using="gin",
        ),
        Index("idx_reflex_rules_tags", postgresql_using="gin"),
        Index("idx_reflex_rules_updated", sql_text("updated_at DESC")),
    )


# ── 11. Deep Dive Steps ──


class DeepDiveStep(Base):
    __tablename__ = "deepdive_steps"

    id: Any = Column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(Text, nullable=False)
    step = Column(Integer, nullable=False)
    step_name = Column(Text, nullable=False)
    base_timeout_sec = Column(Integer, nullable=False)
    min_bound_sec = Column(Integer, nullable=False)
    max_bound_sec = Column(Integer, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at = Column(DateTime(timezone=True))
    elapsed_sec = Column(Integer)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    overrun_count = Column(Integer, nullable=False, server_default=sql_text("0"))
    status = Column(Text, nullable=False, server_default=sql_text("'ACTIVE'"))
    affected_files = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_deepdive_steps_status", "status", "started_at"),
        Index("idx_deepdive_steps_session", "session_id"),
        Index("uq_deepdive_session_step", "session_id", "step", unique=True),
    )


# ── 12. Watchdog Incidents ──


class WatchdogIncident(Base):
    __tablename__ = "watchdog_incidents"

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    dedup_key = Column(Text, nullable=False)
    component = Column(Text, nullable=False)
    status = Column(Text, nullable=False, server_default=sql_text("'open'"))
    symptom = Column(Text)
    context = Column(Text)
    detected_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    action = Column(Text)
    action_result = Column(Text)
    action_at = Column(DateTime(timezone=True))
    resolved_at = Column(DateTime(timezone=True))
    fail_count = Column(Integer, nullable=False, server_default=sql_text("1"))
    reopen_count = Column(Integer, nullable=False, server_default=sql_text("0"))

    __table_args__ = (
        Index("idx_watchdog_incidents_open", "status", "dedup_key"),
        Index("idx_watchdog_incidents_created", sql_text("detected_at DESC")),
    )


# ── 13. Golden Image Versions (from golden_image schema) ──


class GoldenImageVersion(Base):
    __tablename__ = "golden_image_versions"

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Text, nullable=False, unique=True)
    image_id = Column(Text)
    status = Column(Text, nullable=False, server_default=sql_text("'active'"))
    memo = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_golden_versions_status", "status"),
        Index("idx_golden_versions_created", sql_text("created_at DESC")),
    )


class DeploymentLog(Base):
    __tablename__ = "deployment_logs"

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    vm_name = Column(Text, nullable=False)
    version = Column(Text, ForeignKey("golden_image_versions.version"))
    status = Column(Text, nullable=False)
    public_ip = Column(Text)
    error = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_deployment_status", "status"),
        Index("idx_deployment_created", sql_text("created_at DESC")),
    )


class HealthCheck(Base):
    __tablename__ = "health_checks"

    id: Any = Column(Integer, primary_key=True, autoincrement=True)
    deployment_id = Column(Integer, ForeignKey("deployment_logs.id", ondelete="CASCADE"))
    success = Column(Boolean, nullable=False)
    latency_ms = Column(Integer)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_health_deployment", "deployment_id"),
        Index("idx_health_created", sql_text("created_at DESC")),
    )
