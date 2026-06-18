#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Unified LLM client — single entry point for all pipeline scripts.

All DevForge LLM calls go through this module.  It handles:
  - Model registry  (Qwen2.5-Coder-7B → port 8082, …)
  - Feedback auto-injection  (recent patterns from activity_log)
  - Standard HTTP transport  (llama.cpp /v1/chat/completions)

Usage::

    from lib.llm_client import call_llm, call_llm_json, MODEL_REGISTRY

    messages = [
        {"role": "system", "content": "You are a code generator."},
        {"role": "user",   "content": "Build a CLI tool for …"},
    ]
    reply = call_llm(messages, model="reviewer")
    data  = call_llm_json(messages, model="reviewer")
"""

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

# Physical models → port/temp/timeout.
# Role aliases → `_model` key points to physical key.
# Pipeline code only references role keys; change the `_model` value here
# to swap models without touching any pipeline code.

MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # Physical endpoints (role-based — each describes the LLM's primary job)
    "extractor":    {"port": 8082, "temp": 0.12, "max_tokens": 2048, "timeout": 300},  # Pod B 7B Q8
    "polish":       {"port": 8082, "temp": 0.0,  "max_tokens": 512,  "timeout": 600},  # Pod B 4B Q8 — polish phase
    "proposer":     {"port": 8081, "temp": 0.22, "max_tokens": 2048, "timeout": 600},  # Pod B 30B
    "reviewer":     {"port": 8083, "temp": 0.10, "max_tokens": 400,  "timeout": 480},  # Pod B 14B
    "reflector":    {"port": 8082, "temp": 0.10, "max_tokens": 2048, "timeout": 600},  # Pod B 14B
    "verifier":     {"port": 8084, "temp": 0.10, "max_tokens": 4096, "timeout": 1200}, # Pod B 27B
    "judge":        {"port": 8083, "temp": 0.10, "max_tokens": 4096, "timeout": 7200}, # Pod B 14B
    # Non-LLM service endpoints (port-only, for pipeline scripts)
    "reranker":     {"port": 8080},  # Pod A Qwen3-Reranker-4B-Q4_K_M --reranking
    "embedder":     {"port": 8081},  # Pod B embed mode (8B f16) — same port as proposer
    # Role aliases — pipeline code uses these; MODEL_REGISTRY is the single
    # place to change when a model/port changes.
    # Day pipeline — extract (:00/:30)
    "day_extract": {"_model": "extractor"},
    "day_enrich": {"_model": "extractor"},  # day enrich pipeline

    # Day pipeline — verify & rubric
    "day_verify":  {"_model": "reviewer"},

    # Day pipeline — classify (:15/:45) day pre-review — models TBD (Pod B swap)
    "day_proposer":       {"_model": "reviewer"},
    "day_reviewer":       {"_model": "reviewer"},
    "day_judge":          {"_model": "reviewer"},

    # Night pipeline — prj_cycle batch review
    "night_proposer":  {"_model": "proposer"},
    "night_reflector": {"_model": "reflector"},
    "night_judge":     {"_model": "judge"},
    "night_verify":    {"_model": "verifier"},
}


def resolve_model(name: str) -> str:
    """Resolve role alias to physical model name. Pass-through if physical key."""
    cfg = MODEL_REGISTRY.get(name)
    return cfg["_model"] if cfg and "_model" in cfg else name

# How long to cache feedback lookups (seconds)
FEEDBACK_TTL = 300


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
    for m, cfg in MODEL_REGISTRY.items():
        if "_model" in cfg:
            continue  # skip role aliases
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



def call_llm(
    messages: List[Dict[str, str]],
    model: str = "reviewer",
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
        model:       Key in ``MODEL_REGISTRY`` — physical ("reviewer") or role alias ("day_verify").
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
    # Resolve role alias → physical model
    cfg = MODEL_REGISTRY.get(model)
    if not cfg:
        raise ValueError(f"Unknown model: {model}. Known: {list(MODEL_REGISTRY)}")
    if "_model" in cfg:
        model = cfg["_model"]
        cfg = MODEL_REGISTRY[model]

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
    # Cap HTTP timeout at 1800s (30 min) — prevents infinite hang when server is down
    _http_timeout = min(timeout or cfg["timeout"], 1800)
    print(f"  [call_llm] {model}:{port} timeout={_http_timeout}s max_tokens={body['max_tokens']}", flush=True)
    try:
        with urllib.request.urlopen(req, timeout=_http_timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM call to :{port} ({model}) failed: {e}")
    except socket.timeout as e:
        raise RuntimeError(f"LLM call to :{port} ({model}) timed out after {_http_timeout}s")
    elapsed_ms = (time.monotonic() - t_start) * 1000

    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM ({model}) returned no choices: {result}")
    content = (choices[0]["message"].get("content") or "").strip()
    usage = result.get("usage", {})
    timings = result.get("timings", {})

    # Liveness heartbeat — signals that the LLM responder is processing
    try:
        from lib.watchdog.messenger import heartbeat
        heartbeat(f"llm_{model}", detail=f"ok:{elapsed_ms:.0f}ms")
    except Exception:
        pass  # heartbeat is best-effort

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


def reranker_score(query: str, document: str) -> float:
    """Score query-document relevance via Pod A reranker (:8080).

    Used by pipeline stages (extract, enrich, polish_batch) for
    grounding verification — checks that generated content is
    topically relevant to the source text.

    ⚠ This is a RELEVANCE reranker, NOT an NLI model. It measures
    topical relatedness, not logical entailment. High scores mean
    "same topic" not "logically follows." See reranker_nli_verdict()
    for detailed limitations.

    Returns 0.0–1.0 relevance score.
    Returns 0.0 on any error (timeout, connection refused, etc.).
    """
    reranker_port = MODEL_REGISTRY["reranker"]["port"]
    # Truncate to avoid llama.cpp physical batch size limit
    tr = lambda s: s[:2000] if isinstance(s, str) else str(s)[:2000]
    body = json.dumps({
        "query": tr(query),
        "documents": [tr(document)],
        "top_n": 1,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{reranker_port}/v1/rerank", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return float(data["results"][0]["relevance_score"])
    except Exception as e:
        print(f"  [reranker] score call failed: {e}", flush=True)
        return 0.0


def reranker_nli_verdict(score: float) -> str:
    """Map reranker relevance score to grounding verdict.

    ⚠ LIMITATION: This reranker (Qwen3-Reranker-4B Q4_K_M via llama.cpp --reranking)
    performs RELEVANCE scoring, not NLI (Natural Language Inference). It measures
    "how relevant is the document to the query" — NOT logical entailment.

    What it catches well:
      - Topic divergence (completely unrelated content) → UNGROUNDED
      - Wrong entity names (e.g. "MongoDB" vs "PostgreSQL") → UNGROUNDED
      - Unstated claims not found in source → UNGROUNDED

    What it MISSES (false GROUNDED):
      - Negation ("좋다" vs "나쁘다" — same topic, so HIGH)
      - Numerical contradiction ("5100만" vs "1억" — same topic)
      - Partial hallucination (added content that's topically related)
      - Content removal (subset of source text)

    Thresholds (aligned with extract.py Phase 3):
        >= 0.75 → GROUNDED  (confident accept — content is relevant to source)
        >= 0.40 → AMBIGUOUS (somewhat related — accepted, downstream catches)
        <  0.40 → UNGROUNDED (confident reject — content unrelated to source)
    """
    if score >= 0.75:
        return "GROUNDED"
    elif score >= 0.40:
        return "AMBIGUOUS"
    return "UNGROUNDED"


def call_llm_json(
    messages: List[Dict[str, str]],
    model: str = "reviewer",
    **kwargs: Any,
) -> str:
    """Convenience wrapper — same as ``call_llm(…, json_mode=True)``."""
    return call_llm(messages, model, json_mode=True, **kwargs)

