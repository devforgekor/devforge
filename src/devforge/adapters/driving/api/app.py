"""FastAPI application factory for DevForge server.

Creates the FastAPI app with:
  - Health check endpoints
  - MCP SSE streaming (delegates to MCP server)
  - Pipeline trigger endpoints
  - Observation storage endpoints

Usage:
    from devforge.adapters.driving.api.app import create_app
    app = create_app()
    uvicorn app:app --reload
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from devforge.adapters.driven.storage.database_gateway import DatabaseGateway, set_gateway
from devforge.core.config import ConfigRegistry, get_config
from devforge.core.logging import get_logger, setup_logging

logger = get_logger(__name__)


def create_app(config: Optional[ConfigRegistry] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = get_config()

    setup_logging(level="INFO", component="api")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Initialize DB gateway on startup
        gateway = DatabaseGateway.from_config(config)
        set_gateway(gateway)
        logger.info("app_startup", db_url=config.db_url[:50])

        yield

        # Cleanup on shutdown
        await gateway.close()
        logger.info("app_shutdown")

    app = FastAPI(
        title="DevForge API",
        description="AI agent workflow pipeline server API",
        version="1.4.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    # Middleware
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Routes ──

    @app.get("/health")
    async def health() -> Any:
        return {
            "status": "healthy",
            "mode": config.system_mode,
            "inference": config.inference_mode,
            "model": config.model_name,
        }

    @app.get("/api/v1/config")
    async def get_config_endpoint() -> Any:
        """Return non-sensitive config info."""
        return {
            "system_mode": config.system_mode,
            "inference_mode": config.inference_mode,
            "model_name": config.model_name,
            "model_port": config.model_port,
            "providers": list(config.providers.providers.keys()),
            "default_provider": config.providers.default_provider,
        }

    @app.get("/api/v1/turns/{turn_id}")
    async def get_turn(turn_id: str) -> Any:
        """Get a single turn by UUID."""
        from uuid import UUID

        from devforge.adapters.driven.storage.extract_adapter import PostgresTurnRepository

        repo = PostgresTurnRepository.from_config(config)
        turn = await repo.get_by_id(UUID(turn_id))
        if turn is None:
            return {"error": "Turn not found"}, 404
        return {
            "id": str(turn.id),
            "conversation_id": str(turn.conversation_id),
            "seq": turn.seq,
            "user_turn": turn.user_turn,
            "text": turn.text,
            "pipeline_state": turn.pipeline_state,
        }

    @app.get("/api/v1/search")
    async def search_turns(q: str, limit: int = 20, state: Optional[str] = None) -> Any:
        """Search turns via pg_trgm full-text search."""
        from devforge.adapters.driven.storage.extract_adapter import PostgresTurnRepository

        repo = PostgresTurnRepository.from_config(config)
        turns = await repo.search(q, limit=limit, pipeline_state=state)
        return {
            "query": q,
            "count": len(turns),
            "results": [
                {
                    "id": str(t.id),
                    "seq": t.seq,
                    "user_turn": t.user_turn[:200],
                    "pipeline_state": t.pipeline_state,
                }
                for t in turns
            ],
        }

    @app.post("/api/v1/observations")
    async def save_observation(
        observation: str,
        category: str = "general",
        source: str = "qwen_worker",
        context: Optional[dict[str, Any]] = None,
        tags: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Save a worker observation for reflex rule mining."""
        from devforge.adapters.driven.storage.extract_adapter import PostgresObservationRepository

        repo = PostgresObservationRepository.from_config(config)
        obs_id = await repo.save_observation(
            observation=observation,
            category=category,
            source=source,
            context=context,
            tags=tags,
        )
        return {"observation_id": str(obs_id), "saved": True}

    @app.post("/api/v1/ingest")
    async def ingest_endpoint(payload: dict[str, Any]) -> Any:
        """Batch conversation ingestion (HTTP surface).

        Per spec: POST /api/v1/ingest, auth: loopback-only OR bearer token.
        """
        if "source" not in payload:
            return {"error": "source is required"}, 400
        if "conversation_id" not in payload:
            return {"error": "conversation_id is required"}, 400
        if "turns" not in payload:
            return {"error": "turns is required"}, 400

        source = str(payload["source"])[:50]
        agent = str(payload.get("agent") or source)[:50]
        title = str(payload.get("title", "HTTP Ingest"))[:200]
        model = str(payload.get("model", ""))[:100]
        conversation_id = payload.get("conversation_id")
        turns_data = payload.get("turns", [])

        if not turns_data:
            return {"error": "turns array is required"}, 400

        from uuid import UUID, uuid4

        from sqlalchemy import select

        from devforge.adapters.driven.storage.database_gateway import get_gateway
        from devforge.domain.models import Conversation, Turn

        gateway = get_gateway()
        async with gateway.session() as db:
            if conversation_id:
                try:
                    conv_id = UUID(str(conversation_id))
                except ValueError:
                    return {
                        "error": f"Invalid conversation_id (must be UUID): {conversation_id}"
                    }, 400
                conv = await db.get(Conversation, conv_id)
                if conv is None:
                    conv = Conversation(id=conv_id, title=title, source=source, model=model)
                    db.add(conv)
                    await db.flush()
            else:
                conv = Conversation(id=uuid4(), title=title, source=source, model=model)
                db.add(conv)
                await db.flush()
                conv_id = conv.id

            seq_row = await db.execute(
                select(Turn.seq)
                .where(Turn.conversation_id == conv_id)
                .order_by(Turn.seq.desc())
                .limit(1)
            )
            seq = (seq_row.scalar() or 0) + 1

            inserted = 0
            skipped = 0
            seen = set()

            for turn in turns_data:
                smid = turn.get("source_message_id")
                if smid and smid in seen:
                    skipped += 1
                    continue
                if smid:
                    seen.add(smid)

                if smid:
                    existing = await db.execute(
                        select(Turn.id).where(
                            Turn.conversation_id == conv_id,
                            Turn.source_message_id == smid,
                            Turn.source_message_id.is_not(None),
                        )
                    )
                    if existing.scalar() is not None:
                        skipped += 1
                        continue

                db.add(
                    Turn(
                        id=uuid4(),
                        conversation_id=conv_id,
                        seq=seq,
                        user_turn=str(turn.get("user_turn", ""))[:4000],
                        thinking=str(turn.get("thinking", ""))[:4000]
                        if turn.get("thinking")
                        else None,
                        text=str(turn.get("text", ""))[:8000] if turn.get("text") else "",
                        meta_data=turn.get("meta") if isinstance(turn.get("meta"), dict) else {},
                        source_message_id=smid,
                        agent=agent,
                        source=source,
                    )
                )
                await db.flush()
                inserted += 1
                seq += 1

            await db.commit()
            return {
                "conversation_id": str(conv_id),
                "inserted": inserted,
                "skipped": skipped,
            }

    @app.post("/api/v1/pipeline/extract")
    async def trigger_extract(
        turn_id: Optional[str] = None, limit: int = 50, dry_run: bool = False
    ) -> Any:
        """Trigger the extract pipeline.

        - If turn_id is provided, process a single turn.
        - Otherwise, batch process unprocessed turns.
        - Set DEVFORGE_LLM_REPLAY=1 for fixture-based testing.
        """
        from uuid import UUID

        from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter
        from devforge.adapters.driven.storage.extract_adapter import (
            PostgresExtractAdapter,
            PostgresTurnRepository,
        )
        from devforge.application.extract_pipeline import ExtractPipeline

        pipeline = ExtractPipeline(
            llm=LocalLLMAdapter(),
            db=PostgresExtractAdapter.from_config(config),
            turn_repo=PostgresTurnRepository.from_config(config),
            dry_run=dry_run,
        )

        if turn_id:
            result = await pipeline.run_single(UUID(turn_id))
            return {
                "result": {
                    "success": result.success,
                    "facts_extracted": result.facts_extracted,
                    "errors": result.errors,
                }
            }
        else:
            results = await pipeline.run_batch(limit=limit)
            return {
                "results": [
                    {
                        "turn_id": str(r.turn_id),
                        "success": r.success,
                        "facts_extracted": r.facts_extracted,
                    }
                    for r in results
                ]
            }

    # ── Include MCP server routes (SSE) ──
    from devforge.adapters.driving.mcp.server import app as mcp_app

    app.mount("/mcp", mcp_app, name="mcp")

    return app


# ── Module-level app for uvicorn ──
app = create_app()


def main() -> None:
    """Run the FastAPI server."""
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()
