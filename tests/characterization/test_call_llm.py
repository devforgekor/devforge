"""Characterization: lib.llm_client.call_llm request/response contract (Week 2, test 3).

Captures the CURRENT contract so the refactor cannot silently change it:
  - returns the stripped string content by default
  - return_meta=True returns {content, usage, timings, model, elapsed_ms, port}
  - default max_tokens/temperature/serve port come from MODEL_REGISTRY
  - json_mode=True adds response_format={"type": "json_object"}
  - unknown model raises ValueError

No live LLM: urllib is mocked.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from lib.llm_client import MODEL_REGISTRY, call_llm

pytestmark = pytest.mark.characterization

MODEL = "extractor"  # direct registry key (no _model indirection)


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _patcher(payload: dict, captured: dict | None = None):
    def _fake_urlopen(req, timeout=None):
        if captured is not None:
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["url"] = req.full_url
        return _fake_response(payload)

    return [
        patch("lib.llm_client._inject_feedback", lambda messages, model: messages),
        patch("lib.llm_client.urllib.request.urlopen", _fake_urlopen),
    ]


def _run(payload, **kwargs):
    captured = {}
    ctx = _patcher(payload, captured)
    with ctx[0], ctx[1]:
        result = call_llm([{"role": "user", "content": "hi"}], model=MODEL, **kwargs)
    return result, captured


def test_returns_stripped_content():
    payload = {"choices": [{"message": {"content": "  hello world  "}}]}
    result, _ = _run(payload)
    assert result == "hello world"


def test_return_meta_shape():
    payload = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"total_tokens": 3},
        "timings": {"predicted_per_second": 9.9},
    }
    result, _ = _run(payload, return_meta=True)
    assert isinstance(result, dict)
    assert result["content"] == "ok"
    assert result["usage"] == {"total_tokens": 3}
    assert result["model"] == MODEL
    assert result["port"] == MODEL_REGISTRY[MODEL]["port"]
    assert "elapsed_ms" in result


def test_registry_defaults_and_endpoint():
    payload = {"choices": [{"message": {"content": "x"}}]}
    _, captured = _run(payload)
    assert captured["body"]["max_tokens"] == MODEL_REGISTRY[MODEL]["max_tokens"]
    assert captured["body"]["temperature"] == MODEL_REGISTRY[MODEL]["temp"]
    assert captured["body"]["stream"] is False
    assert captured["url"] == f"http://127.0.0.1:{MODEL_REGISTRY[MODEL]['port']}/v1/chat/completions"


def test_json_mode_sets_response_format():
    payload = {"choices": [{"message": {"content": "{}"}}]}
    _, captured = _run(payload, json_mode=True)
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_explicit_params_override_registry():
    payload = {"choices": [{"message": {"content": "x"}}]}
    _, captured = _run(payload, max_tokens=42, temperature=0.0)
    assert captured["body"]["max_tokens"] == 42
    assert captured["body"]["temperature"] == 0.0


def test_unknown_model_raises():
    with pytest.raises(ValueError):
        call_llm([{"role": "user", "content": "hi"}], model="no_such_model")
