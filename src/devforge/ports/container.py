#!/usr/bin/env python3
# Status: experimental
# Path: cli.py → PodmanInferenceAdapter, MCP server, tests
"""Inference container lifecycle port (llama.cpp podman containers)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ContainerHealth:
    """Result of a container health check."""
    port: int
    ok: bool
    model_key: Optional[str] = None
    detail: str = ""


class InferenceContainerManager(ABC):
    """Port: container lifecycle + model identity verification.

    Implementation: PodmanInferenceAdapter (adapters/driven/container/).
    """

    @abstractmethod
    def health(self, port: int, timeout: int = 3) -> bool:
        """Return True if the container on *port* is healthy within *timeout*."""

    @abstractmethod
    def model_identity(self, port: int, model_key: str) -> bool:
        """Return True if the container on *port* serves *model_key*."""

    @abstractmethod
    def ensure_model(self, model_key: str, skip_if_healthy: bool = False) -> bool:
        """Ensure *model_key* is loaded. Return True on success."""

    @abstractmethod
    def switch_mode(
        self,
        mode: str,
        port: int,
        model_key: Optional[str] = None,
    ) -> bool:
        """Switch the container to *mode*. Return True on success."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the inference container."""
