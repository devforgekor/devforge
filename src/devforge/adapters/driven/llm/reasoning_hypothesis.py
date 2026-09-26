#!/usr/bin/env python3
# Status: experimental
# Path: application/error_analysis.py (via cli.py composition root)
"""LLM second stage for error-record §2 (design §4).

Read-only hypothesis generation: prefers the OpenRouter reasoning chain from
`~/.config/devforge/analysis_models.json`, falls back to the local model
(`qwen3-8b`). Inputs are already-masked L2 diagnostics; outputs are parsed
defensively and never executed. Any failure yields no hypotheses (rules stand).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from devforge.core.logging import get_logger
from devforge.ports.error_analysis import Hypothesis

_log = get_logger(__name__)

_DEFAULT_CFG = Path.home() / ".config/devforge/analysis_models.json"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _parse_hypotheses(text: str) -> list[Hypothesis]:
    """Parse an LLM reply into hypotheses; tolerant of fences/prose. Pure."""
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        items = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    out: list[Hypothesis] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict) or not it.get("hypothesis"):
            continue
        try:
            conf = max(0.0, min(1.0, float(it.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        out.append(
            Hypothesis(
                hypothesis=str(it["hypothesis"])[:200],
                confidence=conf,
                rationale=str(it.get("rationale", ""))[:500],
            )
        )
    return out


def _model_chain(data: dict[str, Any]) -> list[str]:
    """OpenRouter model ids (strip the `openrouter/` routing prefix), deduped."""
    raw = [data.get("primary"), *(data.get("chain") or [])]
    out: list[str] = []
    for m in raw:
        if not isinstance(m, str) or not m:
            continue
        model = m.removeprefix("openrouter/")
        if model not in out:
            out.append(model)
    return out


class ReasoningHypothesisClient:
    """HypothesisPort implementation: OpenRouter chain → local fallback."""

    def __init__(self, config_path: Path | None = None, timeout: float = 30.0) -> None:
        self._config_path = config_path or _DEFAULT_CFG
        self._timeout = timeout
        self.model_name = ""

    def _chain(self) -> list[str]:
        try:
            return _model_chain(json.loads(self._config_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return []

    @staticmethod
    def _api_keys() -> list[str]:
        """OpenRouter keys: `OPENROUTER_API_KEY` or account-scoped `OPENROUTER_*_API_KEY`."""
        keys: list[str] = []
        exact = os.environ.get("OPENROUTER_API_KEY")
        if exact:
            keys.append(exact)
        for name in sorted(os.environ):
            value = os.environ.get(name, "")
            if name != "OPENROUTER_API_KEY" and name.startswith("OPENROUTER_") and name.endswith("_API_KEY") and value and value not in keys:
                keys.append(value)
        return keys

    async def hypothesize(self, messages: list[dict[str, str]]) -> list[Hypothesis]:
        chain = self._chain()
        for key in self._api_keys():
            for model in chain:
                self.model_name = model
                parsed = _parse_hypotheses(await self._openrouter(model, key, messages))
                if parsed:
                    return parsed
        self.model_name = "local:day-enricher"
        return _parse_hypotheses(await self._local(messages))

    async def _openrouter(self, model: str, key: str, messages: list[dict[str, str]]) -> str:
        payload = {"model": model, "messages": messages, "temperature": 0.1, "max_tokens": 800}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(_OPENROUTER_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            return data["choices"][0]["message"].get("content") or ""
        except Exception as e:  # noqa: BLE001 — try next model / local fallback
            _log.warning("error_analysis_openrouter_failed", model=model, error=str(e))
            return ""

    async def _local(self, messages: list[dict[str, str]]) -> str:
        try:
            from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter

            result = await LocalLLMAdapter().chat(messages, model_key="day-enricher", json_mode=True)
            return str(result.get("content", ""))
        except Exception as e:  # noqa: BLE001 — no local fallback is not fatal
            _log.warning("error_analysis_local_llm_failed", error=str(e))
            return ""
