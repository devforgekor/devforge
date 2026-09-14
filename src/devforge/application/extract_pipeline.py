"""Extract pipeline service — orchestrates the extract phase.

Phase 1: Extract — SELECT unprocessed turns → LLM extraction → NLI verify → Store
Phase 2: Verify — reranker quality check, status fix
Phase 3: Refine — parallel refinement
Phase 4: Store — DB insert to review_facts

This service implementation uses the hexagonal port/adapter architecture:
- ExtractPort (PostgresExtractAdapter) for DB operations
- LLMPort (LocalLLMAdapter) for LLM interactions
- TurnRepository for turn read operations

When DEVFORGE_LLM_REPLAY is set, uses recorded fixtures instead of live LLM calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from devforge.core.logging import get_logger
from devforge.ports.extract import (
    ExtractedFact,
    ExtractPort,
    LLMPort,
    TurnData,
    TurnRepository,
)

logger = get_logger(__name__)

# ── State constants ──
PIPELINE_STATES = {
    "scanned": "Initial state — not yet processed",
    "pending": "Scheduled for extraction",
    "extracting": "LLM extraction in progress",
    "embedded": "Facts stored, waiting for embedding",
    "verified": "Fully processed, verified",
    "embed_skipped": "Embedding skipped (quality threshold not met)",
}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ExtractResult:
    """Result of extract pipeline for a single turn."""

    turn_id: UUID
    success: bool
    facts_extracted: int = 0
    facts_verified: int = 0
    errors: Optional[list[str]] = None
    elapsed_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


class ExtractPipeline:
    """Orchestrates the extract pipeline stages.

    Usage:
        from devforge.application.extract_pipeline import ExtractPipeline
        from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter
        from devforge.adapters.driven.storage.extract_adapter import PostgresExtractAdapter

        pipeline = ExtractPipeline(
            llm=LocalLLMAdapter(),
            db=PostgresExtractAdapter.from_config(get_config()),
        )
        results = await pipeline.run_batch(limit=50)
    """

    def __init__(
        self,
        llm: LLMPort,
        db: ExtractPort,
        turn_repo: Optional[TurnRepository] = None,
        batch_limit: int = 50,
        dry_run: bool = False,
    ):
        self._llm = llm
        self._db = db
        self._turn_repo = turn_repo
        self._batch_limit = batch_limit
        self.dry_run = dry_run

        # Use recorded fixtures if in replay mode
        self._replay_mode = _env_flag("DEVFORGE_LLM_REPLAY")
        if self._replay_mode:
            logger.info("extract_pipeline_replay_mode", message="Using recorded LLM fixtures")

    async def run_batch(self, limit: int = 50) -> list[ExtractResult]:
        """Run extract pipeline on a batch of unprocessed turns.

        Returns list of per-turn results.
        """
        logger.info("extract_batch_start", limit=limit, dry_run=self.dry_run)

        # Phase 0: Ensure DB schema (read/write DDL — skipped in dry-run)
        if not self.dry_run:
            await self._ensure_schema()

        # Phase 1: Get unprocessed turns
        turns = await self._db.get_unprocessed_turns(limit=limit or self._batch_limit)
        logger.info("extract_batch_turns", count=len(turns))

        results: list[ExtractResult] = []
        for turn in turns:
            result = await self.process_turn(turn)
            results.append(result)

        logger.info(
            "extract_batch_done",
            success=sum(1 for r in results if r.success),
            failed=sum(1 for r in results if not r.success),
        )
        return results

    async def process_turn(self, turn: TurnData) -> ExtractResult:
        """Process a single turn through the extract pipeline."""
        import time

        start = time.monotonic()

        logger.info("process_turn_start", turn_id=str(turn.id), user_turn_len=len(turn.user_turn))

        try:
            # Phase 1: Mark as extracting
            if not self.dry_run:
                await self._db.mark_extracting(turn.id)

            # Phase 2: LLM extraction
            if self._replay_mode:
                facts = await self._replay_extract(turn)
            else:
                from devforge.pipeline_stages.extract.edc import (
                    build_extract_prompt,
                    parse_extract_response,
                )

                messages = build_extract_prompt(turn)
                result = await self._llm.chat(messages, model_key="day_extract", json_mode=True)
                facts = parse_extract_response(result["content"], turn.id, "day_extract")

            logger.info("extract_facts", turn_id=str(turn.id), count=len(facts))

            # Phase 3: NLI verification (batch)
            verified_count = 0
            for fact in facts:
                if not fact.evidence:
                    continue
                verified_count += 1

                # Phase 3a: NLI verify
                if not self.dry_run and len(fact.evidence) > 50:
                    try:
                        verification = await self._llm.verify_claim(
                            claim=fact.evidence[:500],
                            evidence=turn.user_turn[:500],
                        )
                        fact.faithful_score = (
                            0.8 if verification.get("verdict") == "GROUNDED" else 0.3
                        )
                        fact.faithful_method = "nli"
                        fact.grounding = verification.get("verdict", "NEUTRAL")
                    except Exception as e:
                        logger.warning("nli_verify_failed", turn_id=str(turn.id), error=str(e))

            logger.info("facts_verified", turn_id=str(turn.id), count=verified_count)

            # Phase 4: Store to review_facts
            if not self.dry_run and facts:
                stored = await self._db.store_facts(facts)
                logger.info("facts_stored", turn_id=str(turn.id), count=stored)
            elif self.dry_run:
                logger.info("dry_run_skip_store", turn_id=str(turn.id))

            # Phase 5: Advance pipeline state
            if not self.dry_run:
                new_state = "embedded" if facts else "embed_skipped"
                await self._db.set_pipeline_state(turn.id, new_state)

            elapsed_ms = (time.monotonic() - start) * 1000
            return ExtractResult(
                turn_id=turn.id,
                success=True,
                facts_extracted=len(facts),
                facts_verified=verified_count,
                elapsed_ms=elapsed_ms,
            )

        except Exception as e:
            logger.error("process_turn_error", turn_id=str(turn.id), error=str(e))

            # Store error marker
            if not self.dry_run:
                try:
                    await self._db.store_marker(turn.id, f"error: {str(e)[:100]}", "day_extract")
                except Exception:
                    pass

            elapsed_ms = (time.monotonic() - start) * 1000
            return ExtractResult(
                turn_id=turn.id,
                success=False,
                errors=[str(e)],
                elapsed_ms=elapsed_ms,
            )

    async def _replay_extract(self, turn: TurnData) -> list[ExtractedFact]:
        """Use recorded LLM fixture for deterministic testing."""
        import json
        from pathlib import Path

        from devforge.pipeline_stages.extract.edc import parse_extract_response

        fixtures_dir = Path(
            os.environ.get(
                "DEVFORGE_FIXTURE_DIR", "/opt/projects/server/tests/fixtures/llm_recordings"
            )
        )

        fixture_file = fixtures_dir / "extract_llm.json"
        if not fixture_file.exists():
            logger.warning("replay_no_fixture", filename="extract_llm.json")
            return []

        with open(fixture_file) as f:
            entries = json.load(f)

        if not entries:
            return []

        entry = entries[0]
        response = entry.get("response") or entry.get("response_placeholder")

        if response == "REPLAY_NEEDED":
            return self._synthetic_facts(turn)

        if isinstance(response, dict):
            response = json.dumps(response, ensure_ascii=False)
        elif isinstance(response, list):
            response = json.dumps({"facts": response}, ensure_ascii=False)
        elif not isinstance(response, str):
            return self._synthetic_facts(turn)

        return parse_extract_response(response, turn.id, "day_extract")

    def _synthetic_facts(self, turn: TurnData) -> list[ExtractedFact]:
        """Generate synthetic facts for fixture-based testing."""

        text = turn.text or turn.user_turn
        if not text.strip():
            return []

        # Simple fact extraction: split into paragraphs, one fact per paragraph
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        facts = []

        for idx, para in enumerate(paragraphs[:5]):  # Max 5 facts per turn
            facts.append(
                ExtractedFact(
                    turn_id=turn.id,
                    fact_index=idx,
                    fact_type="text",
                    evidence=para[:2000],
                    extract_model="day_extract",
                    subject=None,
                    predicate=None,
                    object_=None,
                    qualifiers=None,
                )
            )

        return facts

    async def _ensure_schema(self) -> None:
        """Ensure DB schema has required columns (idempotent ALTER)."""
        gateway = getattr(self._db, "_gateway", None)
        if gateway is None:
            return

        from sqlalchemy import text as sql_text

        async with gateway.session() as db:
            await db.execute(
                sql_text("ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_verdict TEXT")
            )
            await db.execute(
                sql_text("ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm TEXT")
            )
            await db.execute(
                sql_text("ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS quality_checks JSONB")
            )
            await db.execute(
                sql_text("ALTER TABLE review_facts ADD COLUMN IF NOT EXISTS nli_llm2 TEXT")
            )
            await db.execute(
                sql_text(
                    "ALTER TABLE turns ADD COLUMN IF NOT EXISTS pipeline_state TEXT DEFAULT 'scanned'"
                )
            )
            await db.execute(
                sql_text("ALTER TABLE turns ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'unknown'")
            )

    async def run_single(self, turn_id: UUID) -> ExtractResult:
        """Process a single turn by ID (for debugging)."""
        if self._turn_repo is None:
            from devforge.adapters.driven.storage.extract_adapter import PostgresTurnRepository
            from devforge.core.config import get_config

            self._turn_repo = PostgresTurnRepository.from_config(get_config())

        turn = await self._turn_repo.get_by_id(turn_id)
        if turn is None:
            return ExtractResult(turn_id=turn_id, success=False, errors=["Turn not found"])

        return await self.process_turn(turn)
