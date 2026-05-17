import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .search import save_memory

logger = logging.getLogger(__name__)

router = APIRouter()


class TurnIn(BaseModel):
    user_turn: str
    text: str
    thinking: Optional[str] = None
    source_message_id: Optional[str] = None
    created_at: Optional[str] = None  # ISO 8601 timestamp from parser
    meta: Dict[str, Any] = Field(default_factory=dict)
    type: Optional[str] = None
    tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    tools_used: Optional[List[str]] = None


class IngestRequest(BaseModel):
    source: str
    model: Optional[str] = None
    title: Optional[str] = None
    conversation_id: Optional[str] = None
    turns: List[TurnIn]
    meta: Dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    status: str
    conversation_id: str
    turn_ids: List[str]
    count: int


@router.post("/ingest", response_model=IngestResponse)
async def ingest(body: IngestRequest):
    if not body.turns:
        raise HTTPException(status_code=400, detail="turns is required")

    cid = body.conversation_id
    turn_ids = []

    for turn in body.turns:
        turn_meta = {**body.meta, **turn.meta}
        if turn.type:
            turn_meta["type"] = turn.type
        if turn.tokens is not None:
            turn_meta["tokens"] = turn.tokens
        if turn.latency_ms is not None:
            turn_meta["latency_ms"] = turn.latency_ms
        if turn.tools_used:
            turn_meta["tools_used"] = turn.tools_used
        result = await save_memory(
            source=body.source,
            user_turn=turn.user_turn,
            text=turn.text,
            title=body.title,
            model=body.model,
            thinking=turn.thinking,
            meta=turn_meta,
            conversation_id=cid,
            source_message_id=turn.source_message_id,
            created_at=turn.created_at,
        )
        cid = result["conversation_id"]
        turn_ids.append(result["turn_id"])

    logger.info("Ingested %d turns from %s -> %s", len(turn_ids), body.source, cid)

    return IngestResponse(
        status="ok",
        conversation_id=cid,
        turn_ids=turn_ids,
        count=len(turn_ids),
    )
