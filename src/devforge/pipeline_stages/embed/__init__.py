#!/usr/bin/env python3.12
# Status: experimental
# Path: devforge.cli (W7), devforge.application.day_cycle, tests
"""Embed stage package — enriched -> embedded (D6=A owned stage)."""

from devforge.pipeline_stages.embed.chunking import (
    chunk_text,
    preprocess_for_embed,
    truncate_to_mrl,
)
from devforge.pipeline_stages.embed.stage import EmbedStage, EmbedStageStats

__all__ = ["EmbedStage", "EmbedStageStats", "chunk_text", "preprocess_for_embed", "truncate_to_mrl"]
