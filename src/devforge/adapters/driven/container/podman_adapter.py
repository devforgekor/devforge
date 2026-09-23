#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, inference CLI, orchestrator, tests
"""Podman adapter for inference container lifecycle."""

from __future__ import annotations

import logging
import subprocess
import time
import urllib.request
from typing import Any, Optional

from devforge.domain.model_management.registry import ModelRegistry
from devforge.ports.container import InferenceContainerManager

logger = logging.getLogger(__name__)

INFERENCE_CONTAINER = "devforge-inference"
MODE_FILE = "/opt/ai_data/scripts/current-mode-inference.env"
ENTRYPOINT_SCRIPT = "/opt/ai_data/scripts/inference-entrypoint.sh"

# Legacy podman run args (from lib/pod_manager/container.py)
_INFERENCE_RUN_ARGS = [
    "podman",
    "run",
    "-d",
    "--replace",
    "--name",
    INFERENCE_CONTAINER,
    "--rm",
    "--entrypoint",
    "/bin/bash",
    "--pull",
    "newer",
    "--network",
    "devforge-net",
    "-v",
    "/opt/ai_data/models/gguf:/models:Z",
    "-v",
    f"{ENTRYPOINT_SCRIPT}:/entrypoint.d/inference-entrypoint.sh:Z",
    "-v",
    f"{MODE_FILE}:/entrypoint.d/current-mode.env:Z",
    "--publish",
    "127.0.0.1:8080:8080",
    "--publish",
    "127.0.0.1:8081:8081",
    "--publish",
    "127.0.0.1:8082:8082",
    "--publish",
    "127.0.0.1:8083:8083",
    "--publish",
    "127.0.0.1:8084:8084",
    "--env",
    "SERVER_TIMEOUT=28800",
    "ghcr.io/ggml-org/llama.cpp:server",
    "/entrypoint.d/inference-entrypoint.sh",
]


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
                    "podman",
                    "ps",
                    "--filter",
                    f"name={INFERENCE_CONTAINER}",
                    "--format",
                    "{{.Status}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
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

        return self._start_inference(meta.mode, meta.port, model_key=model_key)

    def switch_mode(self, mode: str, port: int, model_key: Optional[str] = None) -> bool:
        """Switch to *mode* by writing mode env and restarting container."""
        if self._dry_run:
            logger.info("[DRY] switch_mode(%s, port=%d) -> OK", mode, port)
            return True

        self._write_mode_env(mode, port, model_key)
        if model_key:
            return self._start_inference(mode, port, model_key=model_key)
        else:
            # No model key - just restart with mode env
            return self._start_inference(mode, port)

    def stop(self) -> None:
        try:
            subprocess.run(
                ["podman", "rm", "-v", "-f", "-i", INFERENCE_CONTAINER],
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    def _write_mode_env(self, mode: str, port: int, model_key: Optional[str] = None) -> None:
        """Write mode env file for inference container (replicates legacy _write_mode_env)."""
        meta = None
        if model_key:
            meta = self._registry.get(model_key)
        if meta is None:
            # Try to find by port and mode
            for v in self._registry._data.values():
                if v.port == port and (v.mode == mode or v.model_name == mode):
                    meta = v
                    break
        if meta is None:
            meta = self._registry._data.get(mode)
            if meta and meta.port != port:
                meta = None

        entrypoint_mode = meta.mode if meta else mode
        pairs = [("MODE", entrypoint_mode)]
        if meta:
            f = meta.__getattribute__
            pairs += [
                ("MODEL_NAME", f("model_name")),
                ("PORT", str(port)),
                ("MODEL_FILE", meta.file),
                ("CTX_SIZE", str(f("ctx"))),
                ("THREADS", str(f("threads"))),
                ("THREADS_BATCH", str(f("threads_batch"))),
            ]
            for key, env_key in [
                ("cache_ram", "CACHE_RAM"),
                ("mlock", "MLOCK"),
                ("evict_room", "EVICT_ROOM"),
                ("memory_check", "MEMORY_CHECK"),
                ("memory_check_mode", "MEMORY_CHECK_MODE"),
                ("report_memory", "REPORT_MEMORY"),
                ("cache_type_k", "CACHE_TYPE_K"),
                ("cache_type_v", "CACHE_TYPE_V"),
                ("flash_attn", "FLASH_ATTN"),
                ("batch_size", "BATCH_SIZE"),
                ("ubatch_size", "UBATCH_SIZE"),
                ("parallel", "PARALLEL"),
                ("cpus", "CPUS"),
            ]:
                val = getattr(meta, key, None)
                if val is not None and val != "":
                    pairs.append((env_key, str(val)))

        lines = [f"{k}={v}" for k, v in pairs]
        import pathlib

        pathlib.Path(self._mode_env_path).write_text("\n".join(lines) + "\n")
        logger.info("wrote env for %s:%d -> %s", mode, port, meta.file if meta else "?")

    def _start_inference(self, mode: str, port: int, model_key: Optional[str] = None) -> bool:
        """Start inference container with retry logic (replicates legacy start_inference)."""
        if self._dry_run:
            logger.info("[DRY] start_inference(%s, port=%d) -> OK", mode, port)
            return True

        # Write env first
        self._write_mode_env(mode, port, model_key)

        # Stop existing container (kill_all equivalent)
        self.stop()

        # Wait for health with retries (legacy _start_and_wait pattern)
        health_timeout = 600
        ok = self._start_and_wait(port, health_timeout)
        if ok and model_key and not self.model_identity(port, model_key):
            logger.warning(":%d wrong model after start — retrying with env re-write", port)
            self._write_mode_env(mode, port, model_key)
            self.stop()
            ok = self._podman_start()
            if ok:
                ok = self._start_and_wait(port, min(health_timeout, 300))
                if ok:
                    # Retry identity check with backoff
                    for attempt in range(5):
                        if self.model_identity(port, model_key):
                            break
                        wait_time = 10 * (attempt + 1)
                        logger.warning(
                            ":%d model identity check #%d failed — waiting %ds",
                            port,
                            attempt + 1,
                            wait_time,
                        )
                        time.sleep(wait_time)
                        self._write_mode_env(mode, port, model_key)
                    else:
                        logger.error(":%d wrong model after 5 retries — continuing anyway", port)

        if ok:
            logger.info(":%d ready", port)
            time.sleep(5)
        return ok

    def _podman_start(self) -> bool:
        """Execute podman run with legacy args."""
        try:
            r = subprocess.run(_INFERENCE_RUN_ARGS, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                logger.error("podman run failed (rc=%d): %s", r.returncode, r.stderr.strip()[:200])
                return False
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("podman run exception: %s", e)
            return False

    def _start_and_wait(self, port: int, health_timeout: int) -> bool:
        """Start container and wait for health."""
        if not self._podman_start():
            return False
        deadline = time.monotonic() + health_timeout
        while time.monotonic() < deadline:
            if self.health(port, timeout=2):
                return True
            time.sleep(2)
        return False
