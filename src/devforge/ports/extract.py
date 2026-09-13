"""Extract pipeline ports — hexagonal architecture interfaces.

These interfaces define what the domain layer requires from adapters,
keeping domain logic independent of specific LLM backends, storage
details, or container orchestration.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel


@dataclass
class ExtractedFact:
    """A single fact extracted from a conversation turn.

    Schema mirrors review_facts table columns used by extract_pipeline.py.
    """
    turn_id: UUID
    fact_index: int
    fact_type: str
    evidence: str
    extract_model: str
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object_: Optional[str] = None  # 'object' is reserved in Python
    qualifiers: Optional[dict[str, Any]] = None
    faithful_score: Optional[float] = None
    faithful_method: Optional[str] = None
    grounding: Optional[str] = None  # nli_verdict
    nli_llm: Optional[str] = None
    nli_llm2: Optional[str] = None
    source_file: Optional[str] = None
    corrected_evidence: Optional[str] = None
    quality_checks: Optional[dict[str, Any]] = None
    prompt_tokens: Optional[int] = None
    gen_tokens: Optional[int] = None
    elapsed_ms: Optional[float] = None


@dataclass
class TurnData:
    """Raw data for a single conversation turn."""
    id: UUID
    conversation_id: UUID
    seq: int
    user_turn: str
    thinking: Optional[str] = None
    text: str = ""
    pipeline_state: str = "scanned"
    meta: dict[str, Any] = field(default_factory=dict)


class LLMPort(ABC):
    """Abstract port for LLM interactions.

    Adapters implement this for local llama.cpp servers (Track A)
    or cloud providers like OpenAI/Anthropic (Track B).
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, str]],
        model_key: str = "day_extract",
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """Generic chat interface — send messages, get LLM response.

        Prompt building and response parsing are handled by the caller,
        keeping the adapter focused on the LLM HTTP call.
        """
        ...

    @abstractmethod
    async def extract_facts(
        self,
        turn: TurnData,
        model_key: str = "day_extract",
        max_tokens: Optional[int] = None,
    ) -> list[ExtractedFact]:
        """Extract structured facts from a turn via LLM.
        .. deprecated:: Use chat() with build_extract_prompt() instead.
        """
        ...

    @abstractmethod
    async def verify_claim(
        self,
        claim: str,
        evidence: str,
        model_key: str = "day_verify",
    ) -> dict[str, Any]:
        """NLI verification: check if claim is grounded by evidence."""
        ...

    @abstractmethod
    async def enrich_fact(
        self,
        fact: ExtractedFact,
        model_key: str = "day_enrich",
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """Enrich a fact with metadata, embeddings context."""
        ...

    @abstractmethod
    async def rerank(
        self,
        query: str,
        candidates: list[str],
    ) -> list[float]:
        """Rerank search candidates by relevance to query."""
        ...


class ExtractPort(ABC):
    """Abstract port for the extract pipeline.

    The extract pipeline selects unprocessed turns, runs LLM extraction
    on each, verifies with NLI, and stores results.
    """

    @abstractmethod
    async def get_unprocessed_turns(self, limit: int = 50) -> list[TurnData]:
        """Get turns where pipeline_state IN ('scanned', 'pending')
        AND NOT EXISTS in review_facts with matching extract_model."""
        ...

    @abstractmethod
    async def mark_extracting(self, turn_id: UUID) -> bool:
        """Set pipeline_state='extracting' for a turn."""
        ...

    @abstractmethod
    async def store_facts(self, facts: list[ExtractedFact]) -> int:
        """INSERT facts to review_facts with ON CONFLICT upsert.
        Returns count of inserted/updated rows."""
        ...

    @abstractmethod
    async def store_marker(self, turn_id: UUID, mark: str, extract_model: str) -> bool:
        """Insert a marker fact (noise_marker, error, etc)."""
        ...

    @abstractmethod
    async def set_pipeline_state(self, turn_id: UUID, state: str) -> bool:
        """Update pipeline_state for a turn.
        Valid states: scanned, pending, extracting, embedded, verified, embed_skipped."""
        ...


class TurnRepository(ABC):
    """Generic turn repository for read operations."""

    @abstractmethod
    async def get_by_id(self, turn_id: UUID) -> Optional[TurnData]:
        """Retrieve a single turn by UUID."""
        ...

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 20,
        pipeline_state: Optional[str] = None,
    ) -> list[TurnData]:
        """Full-text search across turns."""
        ...

    @abstractmethod
    async def get_recent(
        self,
        limit: int = 50,
        source: Optional[str] = None,
    ) -> list[TurnData]:
        """Get most recent turns."""
        ...


class ObservationRepository(ABC):
    """Repository for Qwen worker observations."""

    @abstractmethod
    async def save_observation(
        self,
        observation: str,
        category: str = "general",
        source: str = "qwen_worker",
        context: Optional[dict[str, Any]] = None,
        tags: Optional[dict[str, Any]] = None,
    ) -> UUID:
        """Save an observation and return its UUID."""
        ...

    @abstractmethod
    async def get_recent_observations(
        self,
        category: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Retrieve recent observations, optionally filtered by category."""
        ...


# ── Parameter models (shared by MCP server and CLI) ──

class PipelineStatusParams(BaseModel):
    """Parameters for pipeline status tool."""
    action: str = "status"
    model_key: Optional[str] = None
