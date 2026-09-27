#!/usr/bin/env python3.12
# Status: experimental
# Path: W7 composition root (devforge.cli pipeline orchestrate)
"""OpenAI-compatible /v1/embeddings HTTP client (sync, transport only — truncation in stage)."""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Optional, Sequence

from devforge.core.logging import get_logger

logger = get_logger(__name__)


class HttpEmbedClient:
    """POST {input, model} to each configured port until one responds."""

    def __init__(
        self, ports: Sequence[int] = (8081,), timeout: int = 600, model: str = "default"
    ) -> None:
        self._ports = list(ports)
        self._timeout = timeout
        self._model = model

    def embed(self, texts: list[str]) -> Optional[list[Optional[list[float]]]]:
        if not texts:
            return []
        for port in self._ports:
            payload = self._post(texts, port)
            if payload is None:
                continue
            try:
                by_index = {int(item["index"]): list(item["embedding"]) for item in payload["data"]}
                return [by_index.get(i) for i in range(len(texts))]
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("embed_response_malformed", port=port, error=str(exc))
                return None
        return None

    def _post(self, texts: list[str], port: int) -> Optional[dict[str, Any]]:
        body = json.dumps({"input": texts, "model": self._model}).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/v1/embeddings",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
                return payload
        except Exception as exc:
            logger.warning("embed_request_failed", port=port, n=len(texts), error=str(exc))
            return None
