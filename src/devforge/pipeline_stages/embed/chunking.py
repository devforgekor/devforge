#!/usr/bin/env python3.12
# Status: experimental
# Path: devforge.pipeline_stages.embed.stage, tests
"""Pure chunking/preprocessing for the embed stage — parity with scripts/pipelines/embed_batch.py."""

from __future__ import annotations

import re
import unicodedata

from devforge.ports.embed import SentenceSplitter, TokenEstimator

CHUNK_MAX_TOKENS = 512
SHORT_TURN_CHARS = 200
EMBED_DIMS = 2048
DEFAULT_OVERLAP_TOKENS = 64


def truncate_to_mrl(vector: list[float], target_dims: int = EMBED_DIMS) -> list[float]:
    truncated = vector[:target_dims]
    norm = sum(x * x for x in truncated) ** 0.5
    if norm > 0:
        truncated = [x / norm for x in truncated]
    return truncated


def preprocess_for_embed(text: str) -> str:
    """NFKC normalize + collapse whitespace before embedding."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _get_tail_sentences(
    sentences: list[str], overlap_tokens: int, estimate_tokens: TokenEstimator
) -> list[str]:
    if overlap_tokens <= 0:
        return []
    tail: list[str] = []
    tok = 0
    for sent in reversed(sentences):
        st = estimate_tokens(sent)
        if tok + st > overlap_tokens:
            break
        tail.insert(0, sent)
        tok += st
    return tail


def chunk_text(
    text: str,
    *,
    split_sentences: SentenceSplitter,
    estimate_tokens: TokenEstimator,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
) -> list[tuple[str, int]]:
    """Sentence-level chunking with token budget and overlap tail carry-over."""
    if len(text) < SHORT_TURN_CHARS:
        return [(text, 0)]
    sentences = split_sentences(text)
    chunks: list[tuple[str, int]] = []
    cur: list[str] = []
    cur_tok = 0
    overlap_sentences: list[str] = []
    for sent in sentences:
        sent_tok = estimate_tokens(sent)
        if sent_tok > CHUNK_MAX_TOKENS:
            if cur:
                chunks.append((" ".join(overlap_sentences + cur), len(chunks)))
                overlap_sentences = _get_tail_sentences(cur, overlap, estimate_tokens)
                cur, cur_tok = [], 0
            max_chars = CHUNK_MAX_TOKENS * 5 // 2
            chunks.append((sent[:max_chars].rstrip(), len(chunks)))
            overlap_sentences = []
        elif cur_tok + sent_tok > CHUNK_MAX_TOKENS:
            if cur:
                chunks.append((" ".join(overlap_sentences + cur), len(chunks)))
            overlap_sentences = _get_tail_sentences(cur, overlap, estimate_tokens)
            cur, cur_tok = [sent], sent_tok
        else:
            cur.append(sent)
            cur_tok += sent_tok
    if cur:
        chunks.append((" ".join(overlap_sentences + cur), len(chunks)))
    if not chunks:
        chunks = [(text[: CHUNK_MAX_TOKENS * 5 // 2], 0)]
    return chunks
