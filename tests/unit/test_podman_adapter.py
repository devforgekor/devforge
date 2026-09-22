#!/usr/bin/env python3
# Status: experimental
# Path: none — test only
"""PodmanInferenceAdapter unit tests (subprocess mock, no real podman)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from devforge.adapters.driven.container.podman_adapter import PodmanInferenceAdapter
from devforge.domain.model_management.metadata import ModelMetadata
from devforge.domain.model_management.registry import ModelRegistry


@pytest.fixture
def registry() -> ModelRegistry:
    return ModelRegistry({
        "day-extractor": ModelMetadata(
            key="day-extractor", file="Qwen3-8B-Q8_0.gguf",
            port=8082, mode="day", model_name="day-extractor",
            ctx=8192, threads=4,
        ),
    })


@pytest.fixture
def adapter(registry: ModelRegistry) -> PodmanInferenceAdapter:
    return PodmanInferenceAdapter(registry, "/tmp/test-mode.env", dry_run=False)


def test_dry_run_ensure_always_succeeds():
    dry = PodmanInferenceAdapter(ModelRegistry({}), "/tmp/x", dry_run=True)
    assert dry.ensure_model("anything") is True


def test_dry_run_switch_always_succeeds():
    dry = PodmanInferenceAdapter(ModelRegistry({}), "/tmp/x", dry_run=True)
    assert dry.switch_mode("day", 8080) is True


def test_health_returns_false_on_no_container(adapter: PodmanInferenceAdapter):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert adapter.health(8082) is False


def test_health_returns_false_on_unhealthy(adapter: PodmanInferenceAdapter):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="Up 5 seconds (unhealthy)", returncode=0)
        assert adapter.health(8082) is False


def test_health_returns_true_on_healthy(adapter: PodmanInferenceAdapter):
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("subprocess.run") as mock_run, \
         patch("urllib.request.urlopen", return_value=mock_resp):
        mock_run.return_value = MagicMock(stdout="Up 5 minutes (healthy)", returncode=0)
        assert adapter.health(8082) is True


def test_stop_removes_container(adapter: PodmanInferenceAdapter):
    with patch("subprocess.run") as mock_run:
        adapter.stop()
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "podman"
        assert "rm" in args


def test_model_identity_checks_v1_models(adapter: PodmanInferenceAdapter):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"data": [{"id": "day-extractor"}]}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert adapter.model_identity(8082, "day-extractor") is True


def test_model_identity_returns_false_on_mismatch(adapter: PodmanInferenceAdapter):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"data": [{"id": "other-model"}]}).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert adapter.model_identity(8082, "day-extractor") is False
