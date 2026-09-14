"""PostgreSQL adapter for extract pipeline ports.

Implements ExtractPort and TurnRepository using async SQLAlchemy
sessions backed by the production devforge_app database.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from devforge.core.config import ConfigRegistry
from devforge.core.logging import get_logger
from devforge.domain.models import ReviewFact, Turn
from devforge.ports.extract import (
    ExtractedFact,
    ExtractPort,
    ObservationRepository,
    TurnData,
    TurnRepository,
)

logger = get_logger(__name__)


class PostgresExtractAdapter(ExtractPort):
    """Async PostgreSQL adapter implementing ExtractPort.

    Replaces the lib.db.psql_json() calls in the legacy extract.py
    with proper async SQLAlchemy sessions.
    """

    def __init__(self, db_url: str):
        from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

        self._gateway = DatabaseGateway(db_url)

    @classmethod
    def from_config(cls, config: ConfigRegistry) -> PostgresExtractAdapter:
        return cls(config.db_url)

    async def get_unprocessed_turns(self, limit: int = 50) -> list[TurnData]:
        """Get turns where pipeline_state IN ('scanned', 'pending')
        AND NOT EXISTS in review_facts with matching extract_model."""
        async with self._gateway.session() as db:
            # NOT EXISTS anti-join: turns not yet extracted by day_extract
            stmt = (
                select(Turn)
                .where(
                    Turn.pipeline_state.in_(["scanned", "pending"]),
                    ~select(ReviewFact.turn_id)
                    .where(ReviewFact.turn_id == Turn.id)
                    .where(ReviewFact.extract_model == "day_extract")
                    .exists(),
                )
                .limit(limit)
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()

            return [
                TurnData(
                    id=row.id,
                    conversation_id=row.conversation_id,
                    seq=row.seq,
                    user_turn=row.user_turn,
                    thinking=row.thinking,
                    text=row.text,
                    pipeline_state=getattr(row, "pipeline_state", "scanned"),
                    meta=dict(row.meta_data) if row.meta_data else {},
                )
                for row in rows
            ]

    async def mark_extracting(self, turn_id: UUID) -> bool:
        """Set pipeline_state='extracting' for a turn."""
        async with self._gateway.session() as db:
            stmt = update(Turn).where(Turn.id == turn_id).values(pipeline_state="extracting")
            await db.execute(stmt)
            return True

    async def store_facts(self, facts: list[ExtractedFact]) -> int:
        """INSERT facts into review_facts with ON CONFLICT upsert."""
        if not facts:
            return 0

        async with self._gateway.session() as db:
            rows = []
            for f in facts:
                rows.append(
                    {
                        "turn_id": str(f.turn_id),
                        "fact_index": f.fact_index,
                        "fact_type": f.fact_type,
                        "evidence": f.evidence,
                        "extract_model": f.extract_model,
                        "source": "extract_pipeline",
                        "fact_action": "extracted",
                        "verdict": "passed",
                        "subject": f.subject,
                        "predicate": f.predicate,
                        "object_": f.object_,
                        "qualifiers": f.qualifiers,
                        "faithful_score": f.faithful_score,
                        "faithful_method": f.faithful_method,
                        "nli_verdict": f.grounding,
                        "nli_llm": f.nli_llm,
                        "nli_llm2": f.nli_llm2,
                        "source_file": f.source_file,
                        "corrected_evidence": f.corrected_evidence,
                        "quality_checks": f.quality_checks,
                        "prompt_tokens": f.prompt_tokens,
                        "gen_tokens": f.gen_tokens,
                        "elapsed_ms": f.elapsed_ms,
                    }
                )

            stmt = pg_insert(ReviewFact).values(rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["turn_id", "fact_index", "extract_model"],
                set_={"evidence": stmt.excluded.evidence},
            )
            await db.execute(stmt)
            return len(facts)

    async def store_marker(self, turn_id: UUID, mark: str, extract_model: str) -> bool:
        """Insert a marker fact (noise_marker, error, etc)."""
        async with self._gateway.session() as db:
            stmt = (
                pg_insert(ReviewFact)
                .values(
                    turn_id=str(turn_id),
                    fact_index=-1,
                    fact_type="marker",
                    evidence=mark,
                    extract_model=extract_model,
                    source="extract_pipeline",
                    fact_action=mark,
                    verdict="passed",
                )
                .on_conflict_do_update(
                    index_elements=["turn_id", "fact_index", "extract_model"],
                    set_={"evidence": mark, "fact_action": mark},
                )
            )
            await db.execute(stmt)
            return True

    async def set_pipeline_state(self, turn_id: UUID, state: str) -> bool:
        """Update pipeline_state for a turn."""
        async with self._gateway.session() as db:
            stmt = update(Turn).where(Turn.id == turn_id).values(pipeline_state=state)
            await db.execute(stmt)
            return True


class PostgresTurnRepository(TurnRepository):
    """Turn read repository for search and retrieval."""

    def __init__(self, db_url: str):
        from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

        self._gateway = DatabaseGateway(db_url)

    @classmethod
    def from_config(cls, config: ConfigRegistry) -> PostgresTurnRepository:
        return cls(config.db_url)

    async def get_by_id(self, turn_id: UUID) -> Optional[TurnData]:
        async with self._gateway.session() as db:
            stmt = select(Turn).where(Turn.id == turn_id)
            result = await db.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return None
            return TurnData(
                id=row.id,
                conversation_id=row.conversation_id,
                seq=row.seq,
                user_turn=row.user_turn,
                thinking=row.thinking,
                text=row.text,
                pipeline_state=row.pipeline_state,
                meta=dict(row.meta_data) if row.meta_data else {},
            )

    async def search(
        self, query: str, limit: int = 20, pipeline_state: Optional[str] = None
    ) -> list[TurnData]:
        """Full-text search using pg_trgm GIN index on turns."""
        async with self._gateway.session() as db:
            ts_query = func.plainto_tsquery("english", query)
            stmt = select(Turn).where(
                func.to_tsvector("english", Turn.user_turn + " " + Turn.text).op("@@")(ts_query),
            )
            if pipeline_state:
                stmt = stmt.where(Turn.pipeline_state == pipeline_state)
            stmt = stmt.limit(limit)
            result = await db.execute(stmt)
            rows = result.scalars().all()

            return [
                TurnData(
                    id=row.id,
                    conversation_id=row.conversation_id,
                    seq=row.seq,
                    user_turn=row.user_turn,
                    thinking=row.thinking,
                    text=row.text,
                    pipeline_state=getattr(row, "pipeline_state", "scanned"),
                    meta=dict(row.meta_data) if row.meta_data else {},
                )
                for row in rows
            ]

    async def get_recent(self, limit: int = 50, source: Optional[str] = None) -> list[TurnData]:
        async with self._gateway.session() as db:
            stmt = select(Turn).order_by(Turn.created_at.desc()).limit(limit)
            if source:
                stmt = stmt.where(Turn.source == source)
            result = await db.execute(stmt)
            rows = result.scalars().all()

            return [
                TurnData(
                    id=row.id,
                    conversation_id=row.conversation_id,
                    seq=row.seq,
                    user_turn=row.user_turn,
                    thinking=row.thinking,
                    text=row.text,
                    pipeline_state=getattr(row, "pipeline_state", "scanned"),
                    meta=dict(row.meta_data) if row.meta_data else {},
                )
                for row in rows
            ]


class PostgresObservationRepository(ObservationRepository):
    """Observation repository for Qwen worker observations."""

    def __init__(self, db_url: str):
        from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

        self._gateway = DatabaseGateway(db_url)

    @classmethod
    def from_config(cls, config: ConfigRegistry) -> PostgresObservationRepository:
        return cls(config.db_url)

    async def save_observation(
        self,
        observation: str,
        category: str = "general",
        source: str = "qwen_worker",
        context: Optional[dict[str, Any]] = None,
        tags: Optional[dict[str, Any]] = None,
    ) -> UUID:
        from sqlalchemy import insert

        from devforge.domain.models import Observation

        async with self._gateway.session() as db:
            stmt = (
                insert(Observation)
                .values(
                    observation=observation,
                    category=category,
                    source=source,
                    context=context or {},
                    tags=tags or {},
                )
                .returning(Observation.id)
            )
            result = await db.execute(stmt)
            return result.scalar_one()  # type: ignore[no-any-return]

    async def get_recent_observations(
        self,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from devforge.domain.models import Observation

        async with self._gateway.session() as db:
            stmt = select(Observation).order_by(Observation.created_at.desc()).limit(limit)
            if category:
                stmt = stmt.where(Observation.category == category)
            result = await db.execute(stmt)
            rows = result.scalars().all()

            return [
                {
                    "id": str(row.id),
                    "observation": row.observation,
                    "category": row.category,
                    "source": row.source,
                    "context": dict(row.context) if row.context else {},
                    "tags": dict(row.tags) if row.tags else {},
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]

    async def search_observations(
        self,
        query: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        from devforge.domain.models import Observation

        async with self._gateway.session() as db:
            stmt = text("""
                SELECT o.id, o.observation, o.category, o.source,
                       o.context, o.tags, o.created_at
                FROM observations o
                WHERE to_tsvector('english', COALESCE(o.observation, ''))
                      @@ plainto_tsquery('english', :query)
                ORDER BY o.created_at DESC
                LIMIT :limit
            """)
            result = await db.execute(stmt, {"query": query, "limit": limit})
            rows = result.fetchall()

            return [
                {
                    "id": str(row.id),
                    "observation": row.observation,
                    "category": row.category,
                    "source": row.source,
                    "context": dict(row.context) if row.context else {},
                    "tags": dict(row.tags) if row.tags else {},
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in rows
            ]
