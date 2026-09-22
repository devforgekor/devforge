#!/usr/bin/env python3
# Status: experimental
# Path: tests/characterization/
"""Characterization test: verify new LLM health adapter matches legacy behavior."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devforge.adapters.driven.health.llm_health import LLMHealthCheck


class TestLLMHealthCheckParity:
    """Verify new LLM health adapter matches legacy checker.py behavior."""

    @patch("httpx.get", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_all_pods_healthy(self, mock_get: AsyncMock) -> None:
        """All pods responding returns ok=True."""
        mock_get.return_value = MagicMock(status_code=200)

        checker = LLMHealthCheck({"pod-a": 11434, "pod-b": 11435})
        result = await checker.check_health()

        assert result.ok is True
        assert "2 pods responding" in result.detail

    @patch("httpx.get", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_one_pod_down(self, mock_get: AsyncMock) -> None:
        """One pod down returns ok=False with pod name."""
        mock_get.side_effect = [
            MagicMock(status_code=200),
            Exception("Connection refused"),
        ]

        checker = LLMHealthCheck({"pod-a": 11434, "pod-b": 11435})
        result = await checker.check_health()

        assert result.ok is False
        assert "pod-b" in result.detail

    @patch("httpx.get", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_non_200_response(self, mock_get: AsyncMock) -> None:
        """Non-200 response treated as failure."""
        mock_get.return_value = MagicMock(status_code=503)

        checker = LLMHealthCheck({"pod-a": 11434})
        result = await checker.check_health()

        assert result.ok is False
        assert "pod-a" in result.detail

    @patch("httpx.get", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_timeout(self, mock_get: AsyncMock) -> None:
        """Timeout treated as failure."""
        mock_get.side_effect = TimeoutError()

        checker = LLMHealthCheck({"pod-a": 11434}, timeout=1)
        result = await checker.check_health()

        assert result.ok is False
        assert "pod-a" in result.detail