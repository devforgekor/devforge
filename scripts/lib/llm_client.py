"""Unified LLM client — single entry point for all pipeline scripts.

All DevForge LLM calls go through this module.  It handles:
  - Model registry  (Qwen3B → port 8082, …)
  - Feedback auto-injection  (recent patterns from activity_log)
  - Standard HTTP transport  (llama.cpp /v1/chat/completions)

Usage::

    from lib.llm_client import call_llm, call_llm_json, MODEL_REGISTRY

    messages = [
        {"role": "system", "content": "You are a code generator."},
        {"role": "user",   "content": "Build a CLI tool for …"},
    ]
    reply = call_llm(messages, model="Qwen3B")
    data  = call_llm_json(messages, model="Qwen30B")
"""

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# ── Model registry ─────────────────────────────────────────────────────────

MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "Qwen3B":  {"port": 8082, "temp": 0.12, "max_tokens": 2048, "timeout": 180},
    "Qwen30B": {"port": 8080, "temp": 0.22, "max_tokens": 2048, "timeout": 600},
    "Qwen7B":  {"port": 8080, "temp": 0.10, "max_tokens": 400,  "timeout": 480},
    "Qwen14B": {"port": 8080, "temp": 0.10, "max_tokens": 2048, "timeout": 600},
    "Qwen27B": {"port": 8081, "temp": 0.10, "max_tokens": 4096, "timeout": 1200},
    "Codestral":{"port": 8080, "temp": 0.10, "max_tokens": 4096, "timeout": 7200},
}

# How long to cache feedback lookups (seconds)
FEEDBACK_TTL = 300

# ── Feedback helpers (deferred import to avoid circular deps) ──────────────

_feedback_cache: Dict[str, List[Dict[str, str]]] = {}
_feedback_ts: float = 0.0


def _get_feedback(model: str) -> List[Dict[str, str]]:
    """Return few-shot messages for *model* from recent activity_log patterns.

    Cached for FEEDBACK_TTL seconds to avoid per-call DB queries.
    """
    global _feedback_cache, _feedback_ts
    now = time.monotonic()
    if (now - _feedback_ts) < FEEDBACK_TTL:
        return _feedback_cache.get(model, [])

    from lib.feedback import get_feedback_for_model

    _feedback_cache = {}
    for m in MODEL_REGISTRY:
        _feedback_cache[m] = get_feedback_for_model(m, max_gold=2, max_edge=2)
    _feedback_ts = now
    return _feedback_cache.get(model, [])


def _inject_feedback(messages: List[Dict], model: str) -> List[Dict]:
    """Insert few-shot feedback between system prompt (if any) and user message.

    If no system message exists, inserts at the beginning.
    """
    fb: List[Dict] = _get_feedback(model)
    if not fb:
        return messages
    # Find the system message and insert feedback after it
    sys_idx = next((i for i, m in enumerate(messages) if m.get("role") == "system"), None)
    if sys_idx is not None:
        return messages[: sys_idx + 1] + fb + messages[sys_idx + 1 :]
    # No system message — insert at the beginning
    return fb + messages


# ── HTTP transport ─────────────────────────────────────────────────────────

def call_llm(
    messages: List[Dict[str, str]],
    model: str = "Qwen3B",
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: Optional[int] = None,
    json_mode: bool = False,
    return_meta: bool = False,
) -> Any:
    """Call a local llama.cpp model and return the response text.

    Args:
        messages:    Chat messages (system + user + …).
        model:       Key in ``MODEL_REGISTRY`` (e.g. ``"Qwen30B"``).
        max_tokens:  Override the registry default.
        temperature: Override the registry default.
        timeout:     HTTP timeout in seconds (override).
        json_mode:   Request ``response_format={"type":"json_object"}``.
        return_meta: If True, returns dict {content, usage, timings, model}
                     instead of just the content string.

    Returns:
        The ``content`` string from the first choice (default).
        Dict with full metadata when ``return_meta=True``.

    Raises:
        RuntimeError: On HTTP failure or empty response.
    """
    cfg = MODEL_REGISTRY.get(model)
    if not cfg:
        raise ValueError(f"Unknown model: {model}. Known: {list(MODEL_REGISTRY)}")

    messages = _inject_feedback(messages, model)

    port = cfg["port"]
    body: Dict[str, Any] = {
        "messages": messages,
        "max_tokens": max_tokens if max_tokens is not None else cfg["max_tokens"],
        "temperature": temperature if temperature is not None else cfg["temp"],
        "stream": False,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    t_start = time.monotonic()
    url = f"http://127.0.0.1:{port}/v1/chat/completions"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or cfg["timeout"]) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM call to :{port} ({model}) failed: {e}")
    elapsed_ms = (time.monotonic() - t_start) * 1000

    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM ({model}) returned no choices: {result}")
    content = choices[0]["message"]["content"].strip()
    usage = result.get("usage", {})
    timings = result.get("timings", {})

    if return_meta:
        return {
            "content": content,
            "usage": usage,
            "timings": timings,
            "model": model,
            "elapsed_ms": elapsed_ms,
            "port": port,
        }
    return content


def call_llm_json(
    messages: List[Dict[str, str]],
    model: str = "Qwen3B",
    **kwargs: Any,
) -> str:
    """Convenience wrapper — same as ``call_llm(…, json_mode=True)``."""
    return call_llm(messages, model, json_mode=True, **kwargs)
