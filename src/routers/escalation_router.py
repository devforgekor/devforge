#!/usr/bin/env python3.12
# Status: experimental
# Path: new extraction pipeline — cloud escalation router
"""4-tier cloud escalation router: OpenRouter free → GitHub Models → opencode Go premium → DeepSeek fallback."""

import json
import os
from typing import Any, Optional

import httpx


class EscalationRouter:
    """4-tier cloud escalation router for extraction pipeline.

    Tier 1 (Free): OpenRouter free router (round-robin)
    Tier 2 (GitHub Models): GitHub Models API (GPT-4o, Phi-3, Llama-3)
    Tier 3 (Premium): opencode Go subscription models
    Tier 4 (Fallback): DeepSeek API direct
    """

    def __init__(
        self,
        openrouter_keys: Optional[list[str]] = None,
        github_token: Optional[str] = None,
        deepseek_key: Optional[str] = None,
        deepseek_base: str = "https://api.deepseek.com/v1",
        opencode_base: str = "http://127.0.0.1:4096/v1",
        opencode_auth: tuple[str, str] = ("user", "pass"),
    ) -> None:
        self.openrouter_keys: list[str] = openrouter_keys or [
            os.environ.get("OPENROUTER_KEY_1") or "",
            os.environ.get("OPENROUTER_KEY_2") or "",
            os.environ.get("OPENROUTER_KEY_3") or "",
        ]
        self.openrouter_keys = [k for k in self.openrouter_keys if k]
        if not self.openrouter_keys:
            raise ValueError("At least one OPENROUTER_KEY required")

        self.github_token = github_token or os.environ.get("GITHUB_TOKEN")
        self.github_models_base = "https://models.inference.ai.azure.com"

        self.deepseek_key = deepseek_key or os.environ.get("DEEPSEEK_API_KEY")
        self.deepseek_base = deepseek_base
        self.opencode_base = opencode_base
        self.opencode_auth = opencode_auth

        self._key_idx = 0

    def _next_openrouter_key(self) -> str:
        key = self.openrouter_keys[self._key_idx]
        self._key_idx = (self._key_idx + 1) % len(self.openrouter_keys)
        return key

    def _is_simple(self, prompt: str) -> bool:
        """Heuristic: simple prompts go to free tier."""
        return len(prompt) < 2000 and "복잡" not in prompt

    async def extract(
        self,
        prompt: str,
        schema: dict[str, Any],
        tier: str = "auto",
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> dict[str, Any]:
        """Extract with automatic or forced tier escalation.

        Args:
            prompt: Input text to extract from
            schema: JSON Schema for structured output
            tier: "free" | "github_models" | "premium" | "fallback" | "auto"
            max_tokens: Max output tokens
            temperature: Sampling temperature

        Returns:
            Parsed JSON extraction result
        """
        if tier == "free" or (tier == "auto" and self._is_simple(prompt)):
            return await self._call_openrouter_free(prompt, schema, max_tokens, temperature)

        if tier == "github_models" or (tier == "auto" and not self._is_simple(prompt)):
            try:
                return await self._call_github_models(prompt, schema, max_tokens, temperature)
            except Exception:
                if tier == "github_models":
                    raise

        if tier == "premium" or tier == "auto":
            try:
                return await self._call_opencode_premium(prompt, schema, max_tokens, temperature)
            except Exception:
                if tier == "premium":
                    raise

        if not self.deepseek_key and (tier == "fallback" or tier == "auto"):
            raise ValueError("DEEPSEEK_API_KEY required for fallback tier")
        return await self._call_deepseek_direct(prompt, schema, max_tokens, temperature)

    async def _call_openrouter_free(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Tier 1: OpenRouter free router (round-robin)."""
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._next_openrouter_key()}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://devforge.local",
                    "X-Title": "DevForge Extraction",
                },
                json={
                    "model": "openrouter/free",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_schema", "json_schema": schema},
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        return self._parse_response(data)

    async def _call_github_models(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Tier 2: GitHub Models API (GPT-4o, Phi-3, Llama-3 via GitHub Models)."""
        if not self.github_token:
            raise ValueError("GITHUB_TOKEN not configured")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.github_models_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.github_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_schema", "json_schema": schema},
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        return self._parse_response(data)

    async def _call_opencode_premium(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Tier 3: opencode Go subscription server (http://localhost:4096/v1)."""
        async with httpx.AsyncClient(timeout=180, auth=self.opencode_auth) as client:
            resp = await client.post(f"{self.opencode_base}/sessions", json={})
            resp.raise_for_status()
            session_id = resp.json()["id"]

            resp = await client.post(
                f"{self.opencode_base}/sessions/{session_id}/prompt",
                json={
                    "prompt": prompt,
                    "model": "openrouter/anthropic/claude-3.5-sonnet",
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                    "responseFormat": {"type": "json_schema", "jsonSchema": schema},
                },
            )
            resp.raise_for_status()
            result = resp.json()
            return self._parse_opencode_result(result)

    async def _call_deepseek_direct(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        """Tier 4: DeepSeek API direct (fallback)."""
        if not self.deepseek_key:
            raise ValueError("DEEPSEEK_API_KEY not configured")
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.deepseek_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.deepseek_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
                timeout=120,
            )
        resp.raise_for_status()
        data = resp.json()
        return self._parse_response(data)

    def _parse_response(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse OpenRouter/DeepSeek standard response."""
        content = data["choices"][0]["message"]["content"]
        return json.loads(content)  # type: ignore[no-any-return]

    def _parse_opencode_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Parse opencode session prompt response."""
        text = result.get("text") or result.get("content") or str(result)
        return json.loads(text)  # type: ignore[no-any-return]
