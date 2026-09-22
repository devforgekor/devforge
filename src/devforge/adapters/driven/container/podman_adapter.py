#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, inference CLI, orchestrator, tests
"""Podman adapter for inference container lifecycle."""
from __future__ import annotations

import logging
import subprocess
import urllib.request
from typing import Any, Optional

from devforge.domain.model_management.metadata import ModelMetadata
from devforge.domain.model_management.registry import ModelRegistry
from devforge.ports.container import InferenceContainerManager

logger = logging.getLogger(__name__)

INFERENCE_CONTAINER = "devforge-inference"


class PodmanInferenceAdapter(InferenceContainerManager):
    """Subprocess/podman implementation of InferenceContainerManager."""

    def __init__(
        self,
        registry: ModelRegistry,
        mode_env_path: str,
        *,
        dry_run: bool = False,
    ) -> None:
        self._registry = registry
        self._mode_env_path = mode_env_path
        self._dry_run = dry_run

    def health(self, port: int, timeout: int = 3) -> bool:
        """Check if the container is running and serving health on *port*."""
        try:
            r = subprocess.run(
                [
                    "podman", "ps",
                    "--filter", f"name={INFERENCE_CONTAINER}",
                    "--format", "{{.Status}}",
                ],
                capture_output=True, text=True, timeout=10,
            )
            status = r.stdout.strip()
            if not status:
                logger.warning("inference container not in podman ps")
                return False
            if "unhealthy" in status:
                logger.warning("inference status=unhealthy")
                return False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return bool(resp.status == 200)
        except (urllib.error.URLError, OSError):
            return False

    def model_identity(self, port: int, model_key: str) -> bool:
        """Return True if the container serves *model_key*."""
        try:
            import json
            req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                import json
                data: Any = json.loads(resp.read())
                models = [m.get("id", "") for m in data.get("data", [])]
                return any(model_key in m for m in models)
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False

    def ensure_model(self, model_key: str, skip_if_healthy: bool = False) -> bool:
        if self._dry_run:
            logger.info("[DRY] ensure_model(%s) -> OK", model_key)
            return True

        meta = self._registry.get(model_key)
        if skip_if_healthy and self.health(meta.port) and self.model_identity(meta.port, model_key):
            logger.info(":%d already healthy and correct model — skip restart", meta.port)
            return True

        self.stop()
        return self._start_container(meta)

    def switch_mode(self, mode: str, port: int, model_key: Optional[str] = None) -> bool:
        """Switch to *mode* by writing mode env and restarting container."""
        if self._dry_run:
            logger.info("[DRY] switch_mode(%s, port=%d) -> OK", mode, port)
            return True

        import pathlib
        pathlib.Path(self._mode_env_path).write_text(
            f"INFERENCE_MODE={mode}\nINFERENCE_PORT={port}\n"
        )
        if model_key:
            self.stop()
            meta = self._registry.get(model_key)
            return self._start_container(meta)
        return True

    def stop(self) -> None:
        try:
            subprocess.run(
                ["podman", "rm", "-f", INFERENCE_CONTAINER],
                capture_output=True, timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _start_container(self, meta: ModelMetadata) -> bool:
        cmd = [
            "podman", "run", "-d",
            "--name", INFERENCE_CONTAINER,
            "--network", "host",
            "-v", f"/opt/ai_data/models/{meta.file}:/models/{meta.file}:ro",
            "ghcr.io/ggml-org/llama.cpp:server",
            "--host", "0.0.0.0",
            "--port", str(meta.port),
            "-m", f"/models/{meta.file}",
            "-c", str(meta.ctx),
            "-t", str(meta.threads),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                logger.error("podman run failed: %s", r.stderr.strip())
                return False
            return self._wait_healthy(meta.port)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("podman run exception: %s", e)
            return False

    def _wait_healthy(self, port: int, timeout: int = 30) -> bool:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.health(port, timeout=2):
                return True
            time.sleep(2)
        return False
