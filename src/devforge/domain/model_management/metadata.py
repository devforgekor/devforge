#!/usr/bin/env python3
# Status: experimental
# Path: cli.py → PodmanInferenceAdapter, inference CLI, tests
"""Pure domain model for inference model metadata."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelMetadata:
    """Static metadata for a single inference model."""

    key: str
    file: str
    port: int
    mode: str  # day|night|embed|rerank|review|verify
    model_name: str
    ctx: int = 8192
    threads: int = 4
    threads_batch: int = 0
    parallel: int = 1
