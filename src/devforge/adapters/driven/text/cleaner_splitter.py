#!/usr/bin/env python3.12
# Status: experimental
# Path: W7 composition root (devforge.cli pipeline orchestrate)
"""Sentence split / token estimate backed by scripts.lib.text_cleaner (injected into EmbedStage)."""

from __future__ import annotations

from lib.text_cleaner import get_cleaner


def split_sentences(text: str) -> list[str]:
    cleaner = get_cleaner()
    lang, _ = cleaner.detect_language(text)
    sentences: list[str] = cleaner.split_sentences(text, lang=lang)
    return sentences


def estimate_tokens(text: str) -> int:
    tokens: int = get_cleaner().estimate_tokens(text)
    return tokens
