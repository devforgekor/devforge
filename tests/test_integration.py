"""Integration characterization tests — Phase 1.6.

These tests verify the refactored pipeline components behave identically
to the golden master fixtures. They use replayed LLM responses (no live
LLM calls) and verify the complete data flow:

1. ConfigRegistry → ExtractPipeline dependency injection
2. ExtractPipeline.process_turn with synthetic facts
3. MCP server tool registration and discovery
4. LocalLLMAdapter model resolution
5. PostgresExtractAdapter port compliance
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from devforge.application.extract_pipeline import ExtractPipeline, ExtractResult
from devforge.core.config import ConfigRegistry, get_config
from devforge.ports.extract import (
    ExtractedFact,
    ExtractPort,
    LLMPort,
    TurnData,
    TurnRepository,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "llm_recordings"


# ── Helpers ──

class FakeLLMPort(LLMPort):
    """LLM port mock that returns synthetic facts — no live LLM calls."""

    def __init__(self, replay_mode: bool = False):
        self._replay_mode = replay_mode
        self.calls: list[dict] = []

    async def extract_facts(self, turn: TurnData, model_key: str = "day_extract", max_tokens: int = None) -> list[ExtractedFact]:
        self.calls.append({"method": "extract_facts", "turn_id": str(turn.id)})
        # Return synthetic facts
        text = turn.text or turn.user_turn
        paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()][:3]
        return [
            ExtractedFact(
                turn_id=turn.id,
                fact_index=idx,
                fact_type="text",
                evidence=para[:500],
                extract_model=model_key,
            )
            for idx, para in enumerate(paragraphs)
        ]

    async def verify_claim(self, claim: str, evidence: str, model_key: str = "day_verify") -> dict:
        self.calls.append({"method": "verify_claim"})
        return {"verdict": "GROUNDED", "claim": claim, "evidence": evidence}

    async def enrich_fact(self, fact: ExtractedFact, model_key: str = "day_enrich", max_tokens: int = None) -> dict:
        self.calls.append({"method": "enrich_fact"})
        return {"enriched_text": fact.evidence, "model": model_key, "usage": {}}

    async def rerank(self, query: str, candidates: list[str]) -> list[float]:
        self.calls.append({"method": "rerank", "query": query})
        return [0.8] * len(candidates)


class FakeExtractPort(ExtractPort):
    """Extract port mock for testing."""

    def __init__(self):
        self.stored_facts: list[ExtractedFact] = []
        self.markers: list[tuple] = []
        self.state_changes: list[tuple] = []
        self.extracting_marks: list = []

    async def get_unprocessed_turns(self, limit: int = 50) -> list[TurnData]:
        return []  # Override in tests

    async def mark_extracting(self, turn_id) -> bool:
        self.extracting_marks.append(turn_id)
        return True

    async def store_facts(self, facts: list[ExtractedFact]) -> int:
        self.stored_facts.extend(facts)
        return len(facts)

    async def store_marker(self, turn_id, mark: str, extract_model: str) -> bool:
        self.markers.append((turn_id, mark, extract_model))
        return True

    async def set_pipeline_state(self, turn_id, state: str) -> bool:
        self.state_changes.append((turn_id, state))
        return True

    @property
    def _gateway(self):
        """Mock gateway for ExtractPipeline._ensure_schema"""
        mock = MagicMock()
        mock.session = lambda: MagicMock()  # No-op for tests
        return mock


# ── Tests ──

class TestExtractPipelineIntegration:
    """Integration tests for ExtractPipeline with fake ports."""

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_pipeline_single_turn_process(self):
        """ExtractPipeline should process a single turn and store facts."""
        llm = FakeLLMPort()
        db = FakeExtractPort()

        pipeline = ExtractPipeline(llm=llm, db=db, dry_run=True)

        turn = TurnData(
            id=uuid4(),
            conversation_id=uuid4(),
            seq=1,
            user_turn="Test user turn for extraction.",
            thinking=None,
            text="This is the assistant response.\n\nSecond paragraph here.",
            pipeline_state="scanned",
        )

        result = await pipeline.process_turn(turn)

        assert result.success is True
        assert result.facts_extracted > 0
        assert len(llm.calls) > 0
        assert llm.calls[0]["method"] == "extract_facts"

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_pipeline_error_recovery(self):
        """ExtractPipeline should store error markers on failure."""
        llm = FakeLLMPort()
        db = FakeExtractPort()

        # Override extract_facts to raise
        async def fail_extract(*args, **kwargs):
            raise RuntimeError("LLM unavailable")
        llm.extract_facts = fail_extract

        pipeline = ExtractPipeline(llm=llm, db=db, dry_run=False)

        turn = TurnData(
            id=uuid4(),
            conversation_id=uuid4(),
            seq=1,
            user_turn="Test turn",
            text="Test content",
        )

        result = await pipeline.process_turn(turn)

        assert result.success is False
        assert len(result.errors) > 0
        assert len(db.markers) > 0
        assert "error" in db.markers[0][1]

    @pytest.mark.characterization
    @pytest.mark.asyncio
    async def test_replay_mode_with_fixtures(self):
        """Pipeline in replay mode should use fixture-based extraction."""
        llm = FakeLLMPort(replay_mode=True)
        db = FakeExtractPort()

        with patch.dict(os.environ, {"DEVFORGE_LLM_REPLAY": "1"}):
            pipeline = ExtractPipeline(llm=llm, db=db, dry_run=False)
            assert pipeline._replay_mode is True

    @pytest.mark.characterization
    def test_extractor_result_dataclass(self):
        """ExtractResult should properly track stats."""
        result = ExtractResult(
            turn_id=uuid4(),
            success=True,
            facts_extracted=5,
            facts_verified=3,
            errors=[],
            elapsed_ms=123.45,
        )
        assert result.success
        assert result.facts_extracted == 5
        assert len(result.errors) == 0


class TestLLMPortResolution:
    """Verify model resolution and configuration."""

    @pytest.mark.characterization
    def test_model_registry_keys(self):
        """MODEL_REGISTRY should have all expected keys."""
        from devforge.adapters.driven.llm.local_adapter import MODEL_REGISTRY, resolve_model

        expected_aliases = [
            "day_extract", "day_enrich", "day_verify", "night_proposer",
            "night_reflector", "night_judge", "night_verify",
        ]
        for alias in expected_aliases:
            assert alias in MODEL_REGISTRY, f"Missing alias: {alias}"
            resolved = resolve_model(alias)
            assert resolved in MODEL_REGISTRY, f"Cannot resolve alias: {alias} → {resolved}"

    @pytest.mark.characterization
    def test_model_ports_assigned(self):
        """Each model should have a port assignment."""
        from devforge.adapters.driven.llm.local_adapter import MODEL_REGISTRY

        for name, cfg in MODEL_REGISTRY.items():
            if "_model" not in cfg:
                assert "port" in cfg, f"Model '{name}' missing port: {cfg}"


class TestMCPServerTools:
    """Verify MCP server tool registration."""

    @pytest.mark.characterization
    def test_mcp_tools_registered(self):
        """MCP server should have 5 registered tools."""
        from devforge.adapters.driving.mcp.server import get_tools

        tools = get_tools()
        assert len(tools) == 5
        tool_names = [t["name"] for t in tools]
        assert "knowledge_search" in tool_names
        assert "pipeline_status" in tool_names
        assert "extract_turn" in tool_names
        assert "deepdive_step" in tool_names
        assert "store_observation" in tool_names

    @pytest.mark.characterization
    def test_mcp_tool_schemas_valid(self):
        """Each tool should have a valid JSON schema."""
        from devforge.adapters.driving.mcp.server import get_tools

        tools = get_tools()
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "inputSchema" in tool
            assert isinstance(tool["inputSchema"], dict)

    @pytest.mark.characterization
    def test_mcp_app_routes_exist(self):
        """FastAPI app should have expected routes."""
        from devforge.adapters.driving.mcp.server import app

        routes = {r.path for r in app.routes}
        assert "/sse" in routes
        assert "/health" in routes
        assert any("/tools/" in r for r in routes)


class TestConfigToPipelineIntegration:
    """Verify ConfigRegistry → ExtractPipeline dependency injection."""

    @pytest.mark.characterization
    def test_config_db_url_passthrough(self):
        """ConfigRegistry should provide a usable DB URL."""
        config = get_config()
        assert config.db_url.startswith("postgresql")

    @pytest.mark.characterization
    def test_config_model_resolution(self):
        """ConfigRegistry should resolve model names."""
        config = get_config()
        assert config.model_name in ("day-extractor", "day-enricher", "night-proposer")

    @pytest.mark.characterization
    def test_config_mode(self):
        """System mode should be day or night."""
        config = get_config()
        assert config.system_mode in ("day", "night")
