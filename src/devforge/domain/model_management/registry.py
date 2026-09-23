#!/usr/bin/env python3
# Status: experimental
# Path: cli.py → inference CLI, orchestrator, tests
"""Inference model registry (pure domain, no I/O)."""

from __future__ import annotations

from typing import Iterator, Mapping

from devforge.domain.model_management.metadata import ModelMetadata


class ModelNotFoundError(KeyError):
    """Raised when a model key is not in the registry."""


class ModelRegistry:
    """Typed registry over MODEL_METADATA (pure, no I/O)."""

    def __init__(self, metadata: Mapping[str, ModelMetadata]) -> None:
        self._data = dict(metadata)

    def get(self, key: str) -> ModelMetadata:
        try:
            return self._data[key]
        except KeyError:
            raise ModelNotFoundError(key) from None

    def by_mode(self, mode: str) -> list[ModelMetadata]:
        return [m for m in self._data.values() if m.mode == mode]

    def keys(self) -> list[str]:
        return list(self._data)

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)
