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

import asyncio
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from devforge.core.config import get_config
from devforge.core.logging import get_logger
from devforge.ports.extract import PipelineStatusParams

logger = get_logger(__name__)


@asynccontextmanager
async def _lifespan(server_app: FastAPI) -> AsyncGenerator[None, None]:
    """Run the Deep Dive hang-detection loop alongside the server lifespan."""
    task = asyncio.create_task(_deepdive_expiry_loop())
    logger.debug("mcp lifespan start: %s", server_app.title)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="DevForge MCP Server",
    description="MCP tools for DevForge AI agent workflows",
    version="1.4.0",
    lifespan=_lifespan,
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


class IngestTurnParams(BaseModel):
    seq: int
    user_turn: str
    thinking: Optional[str] = None
    text: Optional[str] = None
    meta: Optional[dict[str, Any]] = None
    source_message_id: Optional[str] = None


class IngestParams(BaseModel):
    source: str
    agent: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None
    conversation_id: str
    turns: list[IngestTurnParams]


class StoreObservationParams(BaseModel):
    observation: str
    category: str = "general"
    source: str = "qwen_worker"
    context: Optional[dict[str, Any]] = None
    tags: Optional[dict[str, Any]] = None


class DeepDiveStepEnterParams(BaseModel):
    session_id: str
    step: int = 1
    step_name: str = ""
    force: bool = False
    affected_files: Optional[int] = None


class DeepDiveStepExitParams(BaseModel):
    session_id: str
    step: int = 1


class DeepDiveSessionHeartbeatParams(BaseModel):
    session_id: str
    step: int = 1


class DeepDiveSessionStatusParams(BaseModel):
    session_id: str


class DeepDiveVerifySandboxParams(BaseModel):
    session_id: str


class ObsWriteParams(BaseModel):
    observation: str
    source: str = "qwen_worker"
    context: Optional[dict[str, Any]] = None
    tags: Optional[dict[str, Any]] = None


class ObsSearchParams(BaseModel):
    query: str
    source: Optional[str] = None
    limit: int = 20


class SearchTurnsParams(BaseModel):
    query: str
    limit: int = 20
    pipeline_state: Optional[str] = None


class SearchSimilarityParams(BaseModel):
    query: str
    limit: int = 20


class MemSaveParams(BaseModel):
    user_turn: str
    source: str = "claude-code"
    meta: Optional[dict[str, Any]] = None


class MemSearchParams(BaseModel):
    query: str
    limit: int = 20


class GetConversationParams(BaseModel):
    conversation_id: str


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


# ── Deep Dive hang detection (Phase 1 + Phase 2) ──
# Per-step base timeout + min/max bounds; the expiry loop escalates overruns and
# aborts after DEEPDIVE_OVERRUN_LIMIT consecutive checks. Phase 2: when affected_files
# is given, max_bound = base + n*DEEPDIVE_FILE_MARGIN_SEC, clamped to [min, max].

# base recalibrated 2026-09-20 (task #25) from 57 completed deepdive_steps
# (2026-08-25..09-13, 0 overruns): base = ceil30(max(p90*1.5, observed_max));
# min/max kept as safety bounds. Revisit when the sample grows.
DEEPDIVE_STEP_BUDGETS: dict[int, dict[str, Any]] = {
    1: {"name": "yggdrasil_planning", "base": 150, "min": 60, "max": 600},
    2: {"name": "code_explore", "base": 240, "min": 120, "max": 900},
    3: {"name": "lsp_analysis", "base": 450, "min": 120, "max": 1200},
    4: {"name": "external_verify", "base": 420, "min": 120, "max": 900},
    5: {"name": "plan_finalize", "base": 150, "min": 60, "max": 600},
    6: {"name": "implementation", "base": 810, "min": 300, "max": 2400},
    7: {"name": "verification", "base": 180, "min": 120, "max": 900},
}
DEEPDIVE_CHECK_INTERVAL = 60
DEEPDIVE_OVERRUN_LIMIT = 3
DEEPDIVE_FILE_MARGIN_SEC = 120


def _deepdive_effective_max(step: int, affected_files: Optional[int]) -> int:
    """Phase 2 dynamic max_bound. None → static max (Phase 1 compatibility)."""
    budget = DEEPDIVE_STEP_BUDGETS[step]
    if affected_files is None:
        return budget["max"]
    n = max(0, affected_files)
    raw = budget["base"] + n * DEEPDIVE_FILE_MARGIN_SEC
    return min(max(raw, budget["min"]), budget["max"])


def _send_deepdive_alert(text: str) -> bool:
    """Best-effort Slack alert (env: SLACK_BOT_TOKEN_KEY, SLACK_CHANNEL)."""
    token = os.environ.get("SLACK_BOT_TOKEN_KEY", "")
    channel = os.environ.get("SLACK_CHANNEL", "#alerts")
    if not token:
        logger.warning("deepdive alert skipped (no SLACK_BOT_TOKEN_KEY): %s", text)
        return False
    import urllib.request

    payload = json.dumps({"channel": channel, "text": text}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception as e:
        logger.warning("deepdive alert failed: %s", e)
        return False


def _as_aware(dt: Any) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def _deepdive_check_expired() -> list[dict[str, Any]]:
    """ACTIVE steps past their max bound, or heartbeat-stale past base timeout."""
    from sqlalchemy import select

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import DeepDiveStep

    config = get_config()
    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        rows = (await db.scalars(select(DeepDiveStep).where(DeepDiveStep.status == "ACTIVE"))).all()

    now = datetime.now(timezone.utc)
    expired: list[dict[str, Any]] = []
    for r in rows:
        age = (now - _as_aware(r.started_at)).total_seconds()
        stale = (now - _as_aware(r.last_heartbeat_at)).total_seconds()
        if age > r.max_bound_sec:
            reason = "max_bound_exceeded"
        elif age > r.base_timeout_sec and stale > r.base_timeout_sec:
            reason = "heartbeat_stale"
        else:
            continue
        expired.append(
            {
                "id": r.id,
                "session_id": r.session_id,
                "step": r.step,
                "step_name": r.step_name,
                "overrun_count": r.overrun_count,
                "reason": reason,
            }
        )
    return expired


async def _deepdive_escalate(row: dict[str, Any]) -> None:
    from sqlalchemy import update

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import DeepDiveStep

    sid, step, name, reason = row["session_id"], row["step"], row["step_name"], row["reason"]
    overrun = row["overrun_count"] + 1
    config = get_config()
    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        if overrun >= DEEPDIVE_OVERRUN_LIMIT:
            await db.execute(
                update(DeepDiveStep).where(DeepDiveStep.id == row["id"]).values(status="ABORTED")
            )
            await asyncio.to_thread(
                _send_deepdive_alert,
                f"[DeepDive] {sid} step {step}({name}) {overrun}회 연속 초과({reason}) — 세션 자동 중단 (ABORTED)",
            )
        else:
            await db.execute(
                update(DeepDiveStep).where(DeepDiveStep.id == row["id"]).values(overrun_count=overrun)
            )
            await asyncio.to_thread(
                _send_deepdive_alert,
                f"[DeepDive] {sid} step {step}({name}) {overrun}회차 초과({reason}) — 주의 (자동 중단은 3회부터)",
            )


async def _deepdive_expiry_loop() -> None:
    """Every DEEPDIVE_CHECK_INTERVAL seconds, escalate overrun ACTIVE steps."""
    while True:
        try:
            for row in await _deepdive_check_expired():
                await _deepdive_escalate(row)
        except Exception as e:  # noqa: BLE001 — the loop must never die
            logger.warning("deepdive expiry loop error: %s", e)
        await asyncio.sleep(DEEPDIVE_CHECK_INTERVAL)


async def deepdive_step_enter(params: DeepDiveStepEnterParams) -> dict[str, Any]:
    if params.step not in DEEPDIVE_STEP_BUDGETS:
        return {"ok": False, "error": f"step {params.step} not in 1..7"}
    budget = DEEPDIVE_STEP_BUDGETS[params.step]
    name = params.step_name.strip() or budget["name"]
    effective_max = _deepdive_effective_max(params.step, params.affected_files)

    from sqlalchemy import func as sql_func
    from sqlalchemy import insert, select, update

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import DeepDiveStep

    config = get_config()
    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        existing = await db.scalar(
            select(DeepDiveStep).where(
                DeepDiveStep.session_id == params.session_id,
                DeepDiveStep.step == params.step,
            )
        )
        if existing is not None and existing.status == "ABORTED" and not params.force:
            return {
                "ok": False,
                "error": (
                    "step previously ABORTED (반복 hang으로 자동 중단됨) — "
                    "force=true로 원인 확인 후 재진입하세요"
                ),
                "session_id": params.session_id,
                "step": params.step,
            }
        values = {
            "step_name": name,
            "base_timeout_sec": budget["base"],
            "min_bound_sec": budget["min"],
            "max_bound_sec": effective_max,
            "status": "ACTIVE",
            "started_at": sql_func.now(),
            "ended_at": None,
            "elapsed_sec": None,
            "overrun_count": 0,
            "last_heartbeat_at": sql_func.now(),
            "affected_files": params.affected_files,
        }
        if existing is not None:
            await db.execute(update(DeepDiveStep).where(DeepDiveStep.id == existing.id).values(**values))
        else:
            await db.execute(insert(DeepDiveStep).values(session_id=params.session_id, step=params.step, **values))
    return {
        "ok": True,
        "session_id": params.session_id,
        "step": params.step,
        "step_name": name,
        "effective_max_sec": effective_max,
        "affected_files": params.affected_files,
    }


register_tool(
    "deepdive_step_enter",
    "[contract] Enter a deep-dive session step.",
    DeepDiveStepEnterParams,
)


async def deepdive_step_exit(params: DeepDiveStepExitParams) -> dict[str, Any]:
    from sqlalchemy import func as sql_func
    from sqlalchemy import select, update

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import DeepDiveStep

    config = get_config()
    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        existing = await db.scalar(
            select(DeepDiveStep).where(
                DeepDiveStep.session_id == params.session_id,
                DeepDiveStep.step == params.step,
                DeepDiveStep.status == "ACTIVE",
            )
        )
        if existing is None:
            return {
                "ok": False,
                "error": "no ACTIVE step to exit",
                "session_id": params.session_id,
                "step": params.step,
            }
        elapsed = int((datetime.now(timezone.utc) - _as_aware(existing.started_at)).total_seconds())
        await db.execute(
            update(DeepDiveStep).where(DeepDiveStep.id == existing.id).values(
                ended_at=sql_func.now(),
                elapsed_sec=elapsed,
                status="DONE",
                overrun_count=0,
            )
        )
    return {"ok": True, "session_id": params.session_id, "step": params.step, "elapsed_sec": elapsed}


register_tool(
    "deepdive_step_exit",
    "[contract] Exit a deep-dive session step.",
    DeepDiveStepExitParams,
)


async def deepdive_session_heartbeat(params: DeepDiveSessionHeartbeatParams) -> dict[str, Any]:
    from sqlalchemy import func as sql_func
    from sqlalchemy import update

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import DeepDiveStep

    config = get_config()
    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        result = await db.execute(
            update(DeepDiveStep)
            .where(
                DeepDiveStep.session_id == params.session_id,
                DeepDiveStep.step == params.step,
                DeepDiveStep.status == "ACTIVE",
            )
            .values(last_heartbeat_at=sql_func.now())
        )
        heartbeat_ok = bool(getattr(result, "rowcount", 0))
    return {
        "ok": heartbeat_ok,
        "session_id": params.session_id,
        "step": params.step,
    }


register_tool(
    "deepdive_session_heartbeat",
    "[contract] Update deep-dive session heartbeat.",
    DeepDiveSessionHeartbeatParams,
)


async def deepdive_session_status(params: DeepDiveSessionStatusParams) -> dict[str, Any]:
    config = get_config()
    from sqlalchemy import select

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import DeepDiveStep

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        rows = (
            await db.scalars(
                select(DeepDiveStep)
                .where(DeepDiveStep.session_id == params.session_id)
                .order_by(DeepDiveStep.step)
            )
        ).all()

    if not rows:
        return {"session_id": params.session_id, "status": "NONE"}

    aborted = [r.step for r in rows if r.status == "ABORTED"]
    active = next((r for r in rows if r.status == "ACTIVE"), None)
    out: dict[str, Any] = {
        "session_id": params.session_id,
        "steps": [
            {
                "step": r.step,
                "step_name": r.step_name,
                "status": r.status,
                "overrun_count": r.overrun_count,
                "max_bound_sec": r.max_bound_sec,
                "affected_files": r.affected_files,
                "elapsed_sec": r.elapsed_sec,
            }
            for r in rows
        ],
        "has_aborted_step": bool(aborted),
        "aborted_steps": aborted,
    }
    if active is not None:
        out.update(
            {
                "step": active.step,
                "step_name": active.step_name,
                "status": active.status,
                "elapsed_sec": active.elapsed_sec,
            }
        )
    else:
        out["status"] = "NONE"
    return out


register_tool(
    "deepdive_session_status",
    "[contract] Check deep-dive session status.",
    DeepDiveSessionStatusParams,
)


async def deepdive_verify_sandbox(params: DeepDiveVerifySandboxParams) -> dict[str, Any]:
    config = get_config()
    from sqlalchemy import select

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        from devforge.domain.models import DeepDiveStep

        result = await db.execute(
            select(DeepDiveStep)
            .where(
                DeepDiveStep.session_id == params.session_id,
            )
            .order_by(DeepDiveStep.started_at.desc())
            .limit(1)
        )
        step = result.scalar_one_or_none()
        if step is None:
            return {
                "session_id": params.session_id,
                "verified": False,
                "reason": "session_not_found",
            }
        if step.status != "ACTIVE":
            return {
                "session_id": params.session_id,
                "verified": False,
                "reason": f"status={step.status}",
            }
        return {
            "session_id": params.session_id,
            "verified": True,
            "step": step.step,
            "affected_files": step.affected_files,
        }


register_tool(
    "deepdive_verify_sandbox",
    "[contract] Verify deep-dive sandbox state.",
    DeepDiveVerifySandboxParams,
)


async def obs_write(params: ObsWriteParams) -> dict[str, Any]:
    return await store_observation(
        StoreObservationParams(
            observation=params.observation,
            category="general",
            source=params.source,
            context=params.context,
            tags=params.tags,
        )
    )


register_tool(
    "obs_write",
    "[contract] Write an observation (merge target: memory(action=save,kind=obs)).",
    ObsWriteParams,
)


async def obs_search(params: ObsSearchParams) -> dict[str, Any]:
    config = get_config()
    from devforge.adapters.driven.storage.extract_adapter import PostgresObservationRepository

    repo = PostgresObservationRepository.from_config(config)
    results = await repo.search_observations(params.query, limit=params.limit)
    return {"query": params.query, "count": len(results), "results": results}


register_tool(
    "obs_search",
    "[contract] Search observations (merge target: memory(action=search,kind=obs)).",
    ObsSearchParams,
)


async def search_turns(params: SearchTurnsParams) -> dict[str, Any]:
    return await knowledge_search(
        KnowledgeSearchParams(
            query=params.query,
            limit=params.limit,
            pipeline_state=params.pipeline_state,
        )
    )


register_tool(
    "search_turns",
    "[contract] Search conversation turns via pg_trgm.",
    SearchTurnsParams,
)


async def search_similarity(params: SearchSimilarityParams) -> dict[str, Any]:
    config = get_config()
    from sqlalchemy import text

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        stmt = text("""
            SELECT t.id, t.conversation_id, t.seq, t.user_turn, t.text,
                   t.pipeline_state, t.created_at,
                    1 - (t.user_turn <-> :query) AS similarity
            FROM turns t
            WHERE t.user_turn <-> :query < 0.5
            ORDER BY similarity ASC
            LIMIT :limit
        """)
        result = await db.execute(stmt, {"query": params.query, "limit": params.limit})
        turns = []
        for row in result:
            turns.append(
                {
                    "id": str(row.id),
                    "conversation_id": str(row.conversation_id),
                    "seq": row.seq,
                    "user_turn": row.user_turn[:500],
                    "similarity": float(row.similarity) if row.similarity else 0.0,
                }
            )
    return {"query": params.query, "count": len(turns), "results": turns}


register_tool(
    "search_similarity",
    "[contract] Search similar turns via pgvector cosine similarity.",
    SearchSimilarityParams,
)


async def mem_save(params: MemSaveParams) -> dict[str, Any]:
    config = get_config()
    from uuid import uuid4

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import Conversation, Turn

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        conv = Conversation(id=uuid4(), title=f"Memory: {params.source}", source=params.source)
        db.add(conv)
        await db.flush()
        turn = Turn(
            id=uuid4(),
            conversation_id=conv.id,
            seq=1,
            user_turn=params.user_turn[:4000],
            source=params.source,
            meta=params.meta if params.meta else {},
        )
        db.add(turn)
        await db.commit()
    return {"conversation_id": str(conv.id), "turn_id": str(turn.id), "saved": True}


register_tool(
    "mem_save",
    "[contract] Save a memory turn (merge target: memory(action=save,kind=mem)).",
    MemSaveParams,
)


async def mem_search(params: MemSearchParams) -> dict[str, Any]:
    config = get_config()
    from sqlalchemy import text

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        stmt = text("""
            SELECT t.id, t.conversation_id, t.seq, t.user_turn, t.source, t.created_at
            FROM turns t
            WHERE to_tsvector('english', COALESCE(t.user_turn, ''))
                  @@ plainto_tsquery('english', :query)
            ORDER BY t.created_at DESC
            LIMIT :limit
        """)
        result = await db.execute(stmt, {"query": params.query, "limit": params.limit})
        turns = []
        for row in result:
            turns.append(
                {
                    "id": str(row.id),
                    "conversation_id": str(row.conversation_id),
                    "seq": row.seq,
                    "user_turn": row.user_turn[:500],
                    "source": row.source,
                }
            )
    return {"query": params.query, "count": len(turns), "results": turns}


register_tool(
    "mem_search",
    "[contract] Search memory turns.",
    MemSearchParams,
)


async def get_conversation(params: GetConversationParams) -> dict[str, Any]:
    config = get_config()
    from uuid import UUID

    from sqlalchemy import select

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import Conversation, Turn

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        conv_id = UUID(params.conversation_id)
        conv = await db.get(Conversation, conv_id)
        if conv is None:
            return {"error": f"Conversation not found: {conv_id}"}
        result = await db.execute(
            select(Turn).where(Turn.conversation_id == conv_id).order_by(Turn.seq)
        )
        turns = []
        for turn in result.scalars():
            turns.append(
                {
                    "seq": turn.seq,
                    "user_turn": turn.user_turn[:500],
                    "text": (turn.text or "")[:500],
                    "source": turn.source,
                }
            )
    return {
        "conversation_id": str(conv.id),
        "title": conv.title,
        "source": conv.source,
        "model": conv.model,
        "turns": turns,
    }


register_tool(
    "get_conversation",
    "[contract] Get conversation with all turns.",
    GetConversationParams,
)


async def ingest(params: IngestParams) -> dict[str, Any]:
    """Batch conversation ingestion — local agent transcripts + web-LLM capture."""
    config = get_config()
    from uuid import UUID, uuid4

    from sqlalchemy import select

    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.domain.models import Conversation, Turn

    gateway = DatabaseGateway.from_config(config)
    async with gateway.session() as db:
        source = (params.source or "mcp_ingest")[:50]
        agent = (params.agent or source)[:50]
        title = (params.title or "MCP Ingest")[:200]
        model = (params.model or "")[:100]

        if not params.turns:
            return {"error": "turns array is required"}

        if params.conversation_id:
            try:
                conv_id = UUID(params.conversation_id)
            except ValueError:
                return {
                    "error": f"Invalid conversation_id (must be UUID): {params.conversation_id}"
                }
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
        seen_message_ids = set()

        for turn in params.turns:
            source_message_id = turn.source_message_id
            if source_message_id and source_message_id in seen_message_ids:
                skipped += 1
                continue
            if source_message_id:
                seen_message_ids.add(source_message_id)

            existing = await db.execute(
                select(Turn.id).where(
                    Turn.conversation_id == conv_id,
                    Turn.source_message_id == source_message_id,
                    Turn.source_message_id.is_not(None),
                )
            )
            if existing.scalar() is not None:
                skipped += 1
                continue

            turn_obj = Turn(
                id=uuid4(),
                conversation_id=conv_id,
                seq=seq,
                user_turn=(turn.user_turn or "")[:4000],
                thinking=(turn.thinking or "")[:4000] if turn.thinking else None,
                text=(turn.text or "")[:8000] if turn.text else "",
                meta_data=turn.meta if turn.meta else {},
                source_message_id=source_message_id,
                agent=agent,
                source=source,
            )
            db.add(turn_obj)
            await db.flush()
            inserted += 1
            seq += 1

        await db.commit()
        return {
            "conversation_id": str(conv_id),
            "inserted": inserted,
            "skipped": skipped,
        }


register_tool(
    "ingest",
    "Batch conversation ingestion — local agent transcripts + web-LLM capture. "
    "Per spec: POST /api/v1/ingest + MCP ingest dual surfaces.",
    IngestParams,
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
        "deepdive_step_enter": deepdive_step_enter,
        "deepdive_step_exit": deepdive_step_exit,
        "deepdive_session_heartbeat": deepdive_session_heartbeat,
        "deepdive_session_status": deepdive_session_status,
        "deepdive_verify_sandbox": deepdive_verify_sandbox,
        "store_observation": store_observation,
        "get_conversation": get_conversation,
        "obs_search": obs_search,
        "ingest": ingest,
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
        "deepdive_step_enter": DeepDiveStepEnterParams,
        "deepdive_step_exit": DeepDiveStepExitParams,
        "deepdive_session_heartbeat": DeepDiveSessionHeartbeatParams,
        "deepdive_session_status": DeepDiveSessionStatusParams,
        "deepdive_verify_sandbox": DeepDiveVerifySandboxParams,
        "store_observation": StoreObservationParams,
        "get_conversation": GetConversationParams,
        "obs_search": ObsSearchParams,
        "ingest": IngestParams,
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


@app.post("/api/v1/ingest")
async def http_ingest(payload: dict[str, Any]) -> Any:
    """HTTP surface for batch conversation ingestion (spec: /api/v1/ingest).

    Mirrors the MCP `ingest` tool; both surfaces share the same write path.
    Auth: loopback-only OR bearer token (D5-1).
    """
    try:
        params = IngestParams(**payload)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    result = await ingest(params)
    if "error" in result:
        return JSONResponse(status_code=400, content=result)
    return JSONResponse(content=result)


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
