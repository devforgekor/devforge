#!/usr/bin/env python3.12
# Status: experimental
# Path: devforge.pipeline_stages.embed (EmbedStage)
"""Embed pipeline ports — hexagonal interfaces for the embed stage (enriched -> embedded)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence
from uuid import UUID


@dataclass(frozen=True)
class PendingEmbedTurn:
    """A turn awaiting embedding.

    ``user_text``/``resp_text`` carry CLEAN text only (COALESCE of
    *_clean/*_clean_polished). Raw fallback is intentionally excluded —
    missing clean columns are counted, not embedded (quality gate).
    """

    id: UUID
    user_text: str
    resp_text: str
    agent: Optional[str] = None
    model: Optional[str] = None
    created_at: str = ""
    est_chars: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)


class EmbedPort(ABC):
    """Storage operations the embed stage requires (async, prod PostgreSQL)."""

    @abstractmethod
    async def cleanup_orphaned(self) -> int:
        """Delete turn embedding chunks whose turn is not in 'embedded' state."""

    @abstractmethod
    async def dead_letter_short(self) -> int:
        """Move enriched turns with combined clean content < 15 chars to 'embed_skipped'."""

    @abstractmethod
    async def count_clean_missing(self) -> int:
        """Count enriched, unembedded turns whose clean columns are both NULL (skip+count)."""

    @abstractmethod
    async def fetch_pending_turns(self, limit: int) -> list[PendingEmbedTurn]:
        """Return enriched turns without embeddings and with present clean text."""

    @abstractmethod
    async def store_turn_embedding(
        self,
        turn_id: UUID,
        vector: Sequence[float],
        embed_text: str,
        chunk_index: int = 0,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Upsert one embedding chunk (source_type='turn')."""

    @abstractmethod
    async def mark_embedded(self, turn_id: UUID) -> None:
        """Advance pipeline_state to 'embedded'."""

    @abstractmethod
    async def bump_retry(self, turn_id: UUID) -> int:
        """Increment retry_count and return the new value."""


class EmbedClient(Protocol):
    """Embedding backend (OpenAI-compatible /v1/embeddings)."""

    def embed(self, texts: list[str]) -> Optional[list[Optional[list[float]]]]:
        """Return one raw vector per text (None per failed item), or None on batch failure."""
        ...


SentenceSplitter = Callable[[str], list[str]]
TokenEstimator = Callable[[str], int]
