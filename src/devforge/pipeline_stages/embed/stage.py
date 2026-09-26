#!/usr/bin/env python3.12
# Status: experimental
# Path: devforge.cli (pipeline orchestrate, W7), devforge.application.day_cycle
"""EmbedStage — enriched -> embedded pipeline stage (D6=A owned stage)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from devforge.pipeline_stages.embed.chunking import (
    DEFAULT_OVERLAP_TOKENS,
    EMBED_DIMS,
    chunk_text,
    preprocess_for_embed,
    truncate_to_mrl,
)
from devforge.ports.embed import (
    EmbedClient,
    EmbedPort,
    PendingEmbedTurn,
    SentenceSplitter,
    TokenEstimator,
)

BATCH_LIMIT = 50
MAX_BATCH_SIZE = 10
SLOT_CTX = 1600
MIN_CHUNK_CHARS = 15
MAX_CHUNK_CHARS = 8192
SENTINEL_RETRY = 3


@dataclass
class EmbedStageStats:
    orphaned_removed: int = 0
    dead_lettered: int = 0
    clean_missing: int = 0
    turns_pending: int = 0
    turns_embedded: int = 0
    retry_events: int = 0
    turns_empty: int = 0
    chunks_stored: int = 0
    batches: int = 0
    batches_failed: int = 0

    def summary(self) -> str:
        return (
            f"embed: embedded={self.turns_embedded} retry={self.retry_events} "
            f"empty={self.turns_empty} pending={self.turns_pending} "
            f"clean_missing={self.clean_missing} dead_letter={self.dead_lettered} "
            f"orphaned={self.orphaned_removed} chunks={self.chunks_stored} "
            f"batches={self.batches} failed_batches={self.batches_failed}"
        )


def _pack_batches(
    items: list[tuple[PendingEmbedTurn, str, int]],
) -> list[list[tuple[PendingEmbedTurn, str, int]]]:
    batches: list[list[tuple[PendingEmbedTurn, str, int]]] = []
    cur: list[tuple[PendingEmbedTurn, str, int]] = []
    cur_tok = 0
    for item in items:
        text = item[1]
        est = max(1, len(text) * 2 // 3)
        if est > SLOT_CTX:
            if cur:
                batches.append(cur)
                cur, cur_tok = [], 0
            batches.append([item])
        elif cur_tok + est > SLOT_CTX or len(cur) >= MAX_BATCH_SIZE:
            batches.append(cur)
            cur, cur_tok = [item], est
        else:
            cur.append(item)
            cur_tok += est
    if cur:
        batches.append(cur)
    return batches


class EmbedStage:
    """PipelineStage: embed enriched turns (clean-text quality gate, skip+count)."""

    name = "embed"

    def __init__(
        self,
        port: EmbedPort,
        client: EmbedClient,
        *,
        split_sentences: SentenceSplitter,
        estimate_tokens: TokenEstimator,
        limit: int = BATCH_LIMIT,
        overlap: int = DEFAULT_OVERLAP_TOKENS,
    ) -> None:
        self._port = port
        self._client = client
        self._split_sentences = split_sentences
        self._estimate_tokens = estimate_tokens
        self._limit = limit
        self._overlap = overlap

    def run(self) -> str:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run_async())
        raise RuntimeError("EmbedStage.run() called inside a running event loop — use run_async()")

    async def run_async(self) -> str:
        stats = EmbedStageStats()

        stats.orphaned_removed = await self._port.cleanup_orphaned()
        stats.dead_lettered = await self._port.dead_letter_short()
        stats.clean_missing = await self._port.count_clean_missing()

        rows = await self._port.fetch_pending_turns(self._limit)
        stats.turns_pending = len(rows)
        if not rows:
            return stats.summary()

        prepared: list[tuple[PendingEmbedTurn, str, int]] = []
        turn_total: dict[UUID, int] = {}
        for row in rows:
            text = f"{row.user_text} {row.resp_text}".strip()
            if not text:
                stats.turns_empty += 1
                continue
            text = preprocess_for_embed(text)
            n = 0
            for chunk, ci in chunk_text(
                text,
                split_sentences=self._split_sentences,
                estimate_tokens=self._estimate_tokens,
                overlap=self._overlap,
            ):
                if len(chunk) < MIN_CHUNK_CHARS:
                    continue
                prepared.append((row, chunk[:MAX_CHUNK_CHARS], ci))
                n += 1
            if n:
                turn_total[row.id] = n
            else:
                stats.turns_empty += 1
        if not prepared:
            return stats.summary()

        batches = _pack_batches(prepared)
        stats.batches = len(batches)
        progress: dict[UUID, int] = {}

        for batch in batches:
            texts = [text for _, text, _ in batch]
            vectors = await asyncio.to_thread(self._client.embed, texts)
            if vectors is None:
                stats.batches_failed += 1
                continue
            for (row, text, ci), vec in zip(batch, vectors):
                if vec is None:
                    retry = await self._port.bump_retry(row.id)
                    stats.retry_events += 1
                    if retry >= SENTINEL_RETRY:
                        await self._port.store_turn_embedding(
                            row.id,
                            [0.0] * EMBED_DIMS,
                            text or "(sentinel)",
                            0,
                            metadata=row.metadata or None,
                        )
                        await self._port.mark_embedded(row.id)
                        stats.turns_embedded += 1
                    continue
                await self._port.store_turn_embedding(
                    row.id,
                    truncate_to_mrl(vec),
                    text,
                    ci,
                    metadata=row.metadata or None,
                )
                stats.chunks_stored += 1
                progress[row.id] = progress.get(row.id, 0) + 1
                if progress[row.id] >= turn_total[row.id]:
                    await self._port.mark_embedded(row.id)
                    stats.turns_embedded += 1

        return stats.summary()
