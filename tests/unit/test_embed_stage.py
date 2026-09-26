#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/
"""Unit tests for EmbedStage (enriched -> embedded) and pure chunking parity."""

from __future__ import annotations

import re
from typing import Optional
from uuid import uuid4

import pytest

from devforge.pipeline_stages.embed import (
    EmbedStage,
    chunk_text,
    preprocess_for_embed,
    truncate_to_mrl,
)
from devforge.pipeline_stages.embed.stage import _pack_batches
from devforge.ports.embed import EmbedPort, PendingEmbedTurn

LONG_RESP = " ".join(f"Sentence number {i:02d} with enough characters to chunk." for i in range(8))


def fake_split(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


def fake_est(text: str) -> int:
    return max(1, len(text) // 4)


def make_row(user: str = "You are a helpful assistant.", resp: str = LONG_RESP) -> PendingEmbedTurn:
    return PendingEmbedTurn(id=uuid4(), user_text=user, resp_text=resp, agent="main")


class FakeEmbedPort(EmbedPort):
    def __init__(
        self,
        pending: Optional[list[PendingEmbedTurn]] = None,
        *,
        clean_missing: int = 0,
        dead_letter: int = 0,
        orphaned: int = 0,
    ) -> None:
        self.pending = pending or []
        self.embeddings: dict[tuple, list[float]] = {}
        self.retry: dict = {row.id: 0 for row in self.pending}
        self.states: dict = {row.id: "enriched" for row in self.pending}
        self.clean_missing_n = clean_missing
        self.dead_n = dead_letter
        self.orphan_n = orphaned
        self.order: list[str] = []

    async def cleanup_orphaned(self) -> int:
        self.order.append("cleanup")
        return self.orphan_n

    async def dead_letter_short(self) -> int:
        self.order.append("dead")
        return self.dead_n

    async def count_clean_missing(self) -> int:
        self.order.append("count")
        return self.clean_missing_n

    async def fetch_pending_turns(self, limit: int) -> list[PendingEmbedTurn]:
        self.order.append("fetch")
        return [row for row in self.pending if self.states[row.id] == "enriched"][:limit]

    async def store_turn_embedding(
        self,
        turn_id,
        vector,
        embed_text: str,
        chunk_index: int = 0,
        metadata=None,
    ) -> None:
        self.embeddings[(turn_id, chunk_index)] = list(vector)

    async def mark_embedded(self, turn_id) -> None:
        self.states[turn_id] = "embedded"

    async def bump_retry(self, turn_id) -> int:
        self.retry[turn_id] = self.retry.get(turn_id, 0) + 1
        return self.retry[turn_id]


class FakeClient:
    def __init__(self, per_call: Optional[list] = None) -> None:
        self.calls: list[list[str]] = []
        self._queue = list(per_call or [])

    def embed(self, texts: list[str]) -> Optional[list[Optional[list[float]]]]:
        self.calls.append(list(texts))
        if self._queue:
            return self._queue.pop(0)
        return [[0.3, 0.4] for _ in texts]


def make_stage(port: FakeEmbedPort, client: FakeClient, **kw) -> EmbedStage:
    return EmbedStage(port, client, split_sentences=fake_split, estimate_tokens=fake_est, **kw)


class TestEmbedStage:
    async def test_should_mark_turn_embedded_when_all_chunks_stored(self) -> None:
        row = make_row()
        port = FakeEmbedPort([row])
        client = FakeClient()
        summary = await make_stage(port, client).run_async()
        assert "embedded=1" in summary
        assert port.states[row.id] == "embedded"
        assert len(port.embeddings) >= 1

    async def test_should_run_cleanup_dead_count_fetch_in_order(self) -> None:
        port = FakeEmbedPort([make_row()], clean_missing=2, dead_letter=3, orphaned=5)
        summary = await make_stage(port, FakeClient()).run_async()
        assert port.order == ["cleanup", "dead", "count", "fetch"]
        assert "clean_missing=2" in summary
        assert "dead_letter=3" in summary
        assert "orphaned=5" in summary

    async def test_should_report_zero_pending_when_no_rows(self) -> None:
        port = FakeEmbedPort()
        summary = await make_stage(port, FakeClient()).run_async()
        assert "pending=0" in summary
        assert port.embeddings == {}

    async def test_should_count_clean_missing_without_embedding_when_clean_absent(self) -> None:
        port = FakeEmbedPort([], clean_missing=7)
        client = FakeClient()
        summary = await make_stage(port, client).run_async()
        assert "clean_missing=7" in summary
        assert "embedded=0" in summary
        assert client.calls == []

    async def test_should_bump_retry_and_keep_pending_when_vector_null(self) -> None:
        row = make_row()
        port = FakeEmbedPort([row])
        summary = await make_stage(port, FakeClient([[None]])).run_async()
        assert port.retry[row.id] == 1
        assert port.states[row.id] == "enriched"
        assert "embedded=0" in summary

    async def test_should_store_sentinel_and_embed_when_retry_reaches_three(self) -> None:
        row = make_row()
        port = FakeEmbedPort([row])
        port.retry[row.id] = 2
        summary = await make_stage(port, FakeClient([[None]])).run_async()
        assert port.states[row.id] == "embedded"
        assert (row.id, 0) in port.embeddings
        assert len(port.embeddings[(row.id, 0)]) == 2048
        assert all(v == 0.0 for v in port.embeddings[(row.id, 0)])
        assert "embedded=1" in summary

    async def test_should_skip_fetch_and_be_idempotent_when_turn_already_embedded(self) -> None:
        row = make_row()
        port = FakeEmbedPort([row])
        client = FakeClient()
        stage = make_stage(port, client)
        first = await stage.run_async()
        second = await stage.run_async()
        assert "embedded=1" in first
        assert "pending=0" in second
        assert port.order.count("fetch") == 2

    async def test_should_fail_batch_without_state_change_when_client_returns_none(self) -> None:
        row = make_row()
        port = FakeEmbedPort([row])
        summary = await make_stage(port, FakeClient([None])).run_async()
        assert "failed_batches=1" in summary
        assert "embedded=0" in summary
        assert port.embeddings == {}
        assert port.states[row.id] == "enriched"

    async def test_should_count_empty_turn_when_user_and_resp_blank(self) -> None:
        row = make_row(user="", resp="")
        port = FakeEmbedPort([row])
        client = FakeClient()
        summary = await make_stage(port, client).run_async()
        assert "empty=1" in summary
        assert "embedded=0" in summary
        assert client.calls == []

    async def test_should_pack_batches_within_max_size_when_many_turns(self) -> None:
        rows = [make_row(user="hi", resp="short but long enough text to chunk ok.") for _ in range(25)]
        port = FakeEmbedPort(rows)
        client = FakeClient()
        summary = await make_stage(port, client).run_async()
        assert [len(c) for c in client.calls] == [10, 10, 5]
        assert "embedded=25" in summary

    def test_should_return_summary_string_when_run_sync_outside_loop(self) -> None:
        port = FakeEmbedPort([make_row()])
        result = make_stage(port, FakeClient()).run()
        assert "embedded=1" in result

    async def test_should_raise_when_run_sync_inside_running_loop(self) -> None:
        stage = make_stage(FakeEmbedPort(), FakeClient())
        with pytest.raises(RuntimeError, match="run_async"):
            stage.run()

    async def test_should_fail_first_batch_only_when_second_succeeds(self) -> None:
        rows = [make_row(user="hi", resp="short but long enough text to chunk ok.") for _ in range(11)]
        port = FakeEmbedPort(rows)
        client = FakeClient([None])
        summary = await make_stage(port, client).run_async()
        assert "failed_batches=1" in summary
        assert "embedded=1" in summary
        assert "retry=0" in summary


class TestChunking:
    def test_should_return_single_chunk_when_text_shorter_than_two_hundred(self) -> None:
        assert chunk_text("short text", split_sentences=fake_split, estimate_tokens=fake_est) == [("short text", 0)]

    def test_should_carry_overlap_tail_into_next_chunk_when_text_exceeds_budget(self) -> None:
        sentences = [f"Sentence {i:02d} has some words for chunking." for i in range(70)]
        text = " ".join(sentences)
        chunks = chunk_text(text, split_sentences=fake_split, estimate_tokens=fake_est)
        assert len(chunks) >= 2
        carried = [s for s in sentences if s in chunks[0][0] and s in chunks[1][0]]
        assert carried, "next chunk should repeat overlap tail sentences"

    def test_should_split_oversized_sentence_at_max_chars_when_no_boundary(self) -> None:
        text = "x" * 3000
        chunks = chunk_text(text, split_sentences=fake_split, estimate_tokens=fake_est)
        assert chunks[0][0] == text[:1280].rstrip()
        assert sum(len(c) for c, _ in chunks) <= len(text)

    def test_should_normalize_nfkc_and_collapse_whitespace_in_preprocess(self) -> None:
        assert preprocess_for_embed("Ａ\u3000abc\ndef\t!") == "A abc def !"

    def test_should_truncate_and_normalize_to_mrl_dimensions(self) -> None:
        out = truncate_to_mrl([3.0, 4.0])
        assert out == pytest.approx([0.6, 0.8])
        long = truncate_to_mrl([1.0] * 3000)
        assert len(long) == 2048
        assert sum(v * v for v in long) == pytest.approx(1.0)


class TestPackBatches:
    def test_should_isolate_oversized_item_when_estimate_exceeds_slot_ctx(self) -> None:
        row = make_row()
        items = [(row, "a" * 3000, 0), (row, "b" * 10, 0)]
        batches = _pack_batches(items)
        assert [len(b) for b in batches] == [1, 1]

    def test_should_join_small_items_when_estimate_fits_slot_ctx(self) -> None:
        row = make_row()
        items = [(row, "a" * 30, i) for i in range(4)]
        batches = _pack_batches(items)
        assert len(batches) == 1
        assert len(batches[0]) == 4
