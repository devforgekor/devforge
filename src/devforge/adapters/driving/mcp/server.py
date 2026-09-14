"""MCP (Model Context Protocol) server for DevForge.

Provides tools to the host LLM (Claude Code, Copilot CLI) for:
  - knowledge_search: Search conversation turns via pg_trgm
  - pipeline_status: Check pipeline_state distribution
  - deepdive: Start/enter/verify deep-dive sessions
  - extract_turn: Extract facts from a single turn
  - store_observation: Save Qwen worker observations

Architecture:
  - FastAPI app exposes tools via SSE (for Python 3.9 compatibility)
  - Each tool is a standalone function with typed parameters
  - Uses PostgresExtractAdapter + LocalLLMAdapter as backend
  - Tools return JSON-serializable results

Usage:
  devforge mcp serve --host 0.0.0.0 --port 8100

In Claude Code:
  "mcpServers": {"devforge": {"url": "http://localhost:8100/sse"}}
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from devforge.core.config import get_config
from devforge.core.logging import get_logger
from devforge.ports.extract import PipelineStatusParams

logger = get_logger(__name__)

app = FastAPI(
    title="DevForge MCP Server",
    description="MCP tools for DevForge AI agent workflows",
    version="1.4.0",
)


# ── Tool schemas ──


class KnowledgeSearchParams(BaseModel):
    query: str
    limit: int = 20
    pipeline_state: Optional[str] = None


class ExtractTurnParams(BaseModel):
    turn_id: str
    model_key: str = "day_extract"
    max_tokens: Optional[int] = None


class DeepDiveParams(BaseModel):
    session_id: str
    step: int = 1
    step_name: str = ""
    base_timeout_sec: int = 300
    affected_files: Optional[int] = None


class StoreObservationParams(BaseModel):
    observation: str
    category: str = "general"
    source: str = "qwen_worker"
    context: Optional[dict[str, Any]] = None
    tags: Optional[dict[str, Any]] = None


# ── Tool registry ──

_TOOLS: list[dict[str, Any]] = []


def register_tool(name: str, description: str, params_cls: type[BaseModel]) -> None:
    """Register a tool for discovery."""
    _TOOLS.append(
        {
            "name": name,
            "description": description,
            "inputSchema": params_cls.model_json_schema(),
        }
    )


def get_tools() -> list[dict[str, Any]]:
    """Return list of available tools for MCP discovery."""
    return _TOOLS


# ── Tool implementations ──


async def knowledge_search(params: KnowledgeSearchParams) -> dict[str, Any]:
    """Search conversation turns via pg_trgm full-text search."""
    config = get_config()
    from sqlalchemy import text

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

    gateway = DatabaseGateway.from_config(config)

    async with gateway.session() as db:
        # Use pg_trgm search
        stmt = text("""
            SELECT t.id, t.conversation_id, t.seq, t.user_turn, t.text,
                   t.pipeline_state, t.created_at
            FROM turns t
            WHERE to_tsvector('english', COALESCE(t.user_turn, '') || ' ' || COALESCE(t.text, ''))
                  @@ plainto_tsquery('english', :query)
        """)
        if params.pipeline_state:
            stmt = text(stmt.text + " AND pipeline_state = :pipeline_state")

        stmt = text(stmt.text + f" ORDER BY t.created_at DESC LIMIT {params.limit}")

        result = await db.execute(
            stmt,
            {
                "query": params.query,
                "pipeline_state": params.pipeline_state,
            },
        )

        turns = []
        for row in result:
            turns.append(
                {
                    "id": str(row.id),
                    "conversation_id": str(row.conversation_id),
                    "seq": row.seq,
                    "user_turn": row.user_turn[:500],
                    "text": row.text[:500],
                    "pipeline_state": row.pipeline_state,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )

    return {"query": params.query, "results": turns, "count": len(turns)}


register_tool(
    "knowledge_search",
    "Search conversation turns by full-text query using pg_trgm indexes. "
    "Returns matching turns with pipeline state.",
    KnowledgeSearchParams,
)


async def pipeline_status(params: PipelineStatusParams) -> dict[str, Any]:
    """Check pipeline state distribution and model status."""
    config = get_config()
    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

    gateway = DatabaseGateway.from_config(config)

    async with gateway.session() as db:
        # Pipeline state distribution
        from sqlalchemy import func as sql_func
        from sqlalchemy import select

        from devforge.domain.models import Turn

        stmt = (
            select(
                Turn.pipeline_state,
                sql_func.count().label("count"),
                sql_func.sum(sql_func.length(Turn.text)).label("total_chars"),
            )
            .group_by(Turn.pipeline_state)
            .order_by(sql_func.count().desc())
        )
        result = await db.execute(stmt)
        states = [
            {
                "state": row.pipeline_state,
                "count": row.count,
                "chars": int(row.total_chars) if row.total_chars else 0,
            }
            for row in result
        ]

        # Total turns
        total = await db.scalar(select(sql_func.count()).select_from(Turn))

    # Model status from runtime env
    runtime = config.runtime
    model_status = {
        "mode": runtime.MODE,
        "model_name": runtime.MODEL_NAME,
        "port": runtime.PORT,
        "model_file": runtime.MODEL_FILE,
        "ctx_size": runtime.CTX_SIZE,
    }

    # Embedding stats from golden master
    golden_path = config.paths.server_dir / "data" / "golden_master.json"
    embed_stats = None
    if golden_path.exists():
        import json

        with open(golden_path) as f:
            golden = json.load(f)
        embed_stats = golden.get("embedding_stats", {})

    return {
        "total_turns": total,
        "state_distribution": states,
        "model_status": model_status,
        "embedding_stats": embed_stats,
    }


register_tool(
    "pipeline_status",
    "Check pipeline state distribution across turns, model status, and embedding stats.",
    PipelineStatusParams,
)


_pipeline_factory: Any = None


def set_pipeline_factory(factory: Any) -> None:
    """Set the pipeline factory function (called by app bootstrap in cli.py)."""
    global _pipeline_factory
    _pipeline_factory = factory


def get_pipeline() -> Any:
    """Get an ExtractPipeline instance via factory.

    The factory is set by the composition root (`devforge.cli`), which is the
    entry point for `devforge mcp serve`. Adapters never import the application
    layer directly (enforced by import-linter).
    """
    if _pipeline_factory is not None:
        return _pipeline_factory()
    raise RuntimeError(
        "Pipeline factory not set. Run the MCP server via `devforge mcp serve` "
        "(or call set_pipeline_factory() during startup)."
    )


async def extract_turn(params: ExtractTurnParams) -> dict[str, Any]:
    """Extract facts from a single turn using the specified model."""
    from uuid import UUID

    pipeline = get_pipeline()
    turn_id = UUID(params.turn_id)
    result = await pipeline.run_single(turn_id)

    return {
        "success": result.success,
        "facts_extracted": result.facts_extracted,
        "facts_verified": result.facts_verified,
        "elapsed_ms": result.elapsed_ms,
        "errors": result.errors,
    }


register_tool(
    "extract_turn",
    "Run the extract pipeline on a single turn by UUID. Returns extracted fact count.",
    ExtractTurnParams,
)


async def deepdive(params: DeepDiveParams) -> dict[str, Any]:
    """Manage deep-dive session steps — heartbeat, enter, verify."""
    config = get_config()
    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

    gateway = DatabaseGateway.from_config(config)

    async with gateway.session() as db:
        from sqlalchemy import func as sql_func
        from sqlalchemy import insert, select, update

        from devforge.domain.models import DeepDiveStep

        # Check existing ACTIVE step
        existing = await db.scalar(
            select(DeepDiveStep).where(
                DeepDiveStep.session_id == params.session_id,
                DeepDiveStep.step == params.step,
                DeepDiveStep.status == "ACTIVE",
            )
        )

        if existing:
            # Heartbeat update
            await db.execute(
                update(DeepDiveStep)
                .where(DeepDiveStep.id == existing.id)
                .values(last_heartbeat_at=sql_func.now())
            )
            result: dict[str, Any] = {
                "action": "heartbeat",
                "step": existing.step,
                "step_name": existing.step_name,
                "elapsed_sec": existing.elapsed_sec,
                "overrun_count": existing.overrun_count,
                "status": existing.status,
            }
        else:
            # Enter new step
            await db.execute(
                insert(DeepDiveStep).values(
                    session_id=params.session_id,
                    step=params.step,
                    step_name=params.step_name or f"step_{params.step}",
                    base_timeout_sec=params.base_timeout_sec,
                    min_bound_sec=int(params.base_timeout_sec * 0.5),
                    max_bound_sec=params.base_timeout_sec * 10,
                    affected_files=params.affected_files,
                )
            )
            result = {
                "action": "enter",
                "step": params.step,
                "step_name": params.step_name or f"step_{params.step}",
                "status": "ACTIVE",
            }

    return result


register_tool(
    "deepdive_step",
    "Manage deep-dive session steps. Enter a new step or update heartbeat. "
    "Returns ACTIVE step info or creates new step.",
    DeepDiveParams,
)


async def store_observation(params: StoreObservationParams) -> dict[str, Any]:
    """Save an observation to the database for reflex rule mining."""
    config = get_config()
    from devforge.adapters.driven.storage.extract_adapter import PostgresObservationRepository

    repo = PostgresObservationRepository.from_config(config)
    observation_id = await repo.save_observation(
        observation=params.observation,
        category=params.category,
        source=params.source,
        context=params.context,
        tags=params.tags,
    )

    return {"observation_id": str(observation_id), "saved": True}


register_tool(
    "store_observation",
    "Save an observation to the database for reflex rule mining. "
    "Observations are used to detect patterns and trigger auto-fix rules.",
    StoreObservationParams,
)


# ── FastAPI routes ──


@app.get("/sse")
async def sse_endpoint() -> Any:
    """MCP protocol: SSE endpoint for tool discovery."""

    async def event_generator() -> AsyncIterator[str]:
        # Send tool list as initial message
        yield f"data: {json.dumps({'type': 'tools', 'tools': get_tools()})}\n\n"
        # Keep connection open
        import asyncio

        while True:
            await asyncio.sleep(30)
            yield "data: \n\n"

    return EventSourceResponse(event_generator())


@app.post("/tools/{tool_name}")
async def call_tool(tool_name: str, params: Optional[dict[str, Any]] = None) -> Any:
    """MCP protocol: Call a specific tool with parameters."""
    params = params or {}

    tool_funcs = {
        "knowledge_search": knowledge_search,
        "pipeline_status": pipeline_status,
        "extract_turn": extract_turn,
        "deepdive_step": deepdive,
        "store_observation": store_observation,
    }

    if tool_name not in tool_funcs:
        return JSONResponse(
            status_code=404,
            content={"error": f"Unknown tool: {tool_name}"},
        )

    # Validate params against schema
    schemas = {
        "knowledge_search": KnowledgeSearchParams,
        "pipeline_status": PipelineStatusParams,
        "extract_turn": ExtractTurnParams,
        "deepdive_step": DeepDiveParams,
        "store_observation": StoreObservationParams,
    }

    try:
        validated = schemas[tool_name](**params)
        result = await tool_funcs[tool_name](validated)  # type: ignore[operator]
        return JSONResponse(content={"result": result})
    except Exception as e:
        logger.error("mcp_tool_error", tool=tool_name, error=str(e))
        return JSONResponse(
            status_code=500,
            content={"error": str(e), "tool": tool_name},
        )


@app.get("/health")
async def health() -> Any:
    """Health check endpoint."""
    return {"status": "healthy", "tools_count": len(_TOOLS)}


# ── CLI entry ──


def main() -> None:
    """Run MCP server via uvicorn."""
    import argparse

    parser = argparse.ArgumentParser(description="DevForge MCP Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
