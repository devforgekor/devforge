#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Unified LLM client — single entry point for all pipeline scripts.

All DevForge LLM calls go through this module.  It handles:
  - Model registry with role-based model resolution
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
import threading
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
    "extractor":    {"port": 8082, "temp": 0.12, "max_tokens": 2048, "timeout": 300},  # Pod B extractor
    "polisher":     {"port": 8080, "temp": 0.0,  "max_tokens": 512,  "timeout": 600},  # Pod A router
    "proposer":     {"port": 8081, "temp": 0.22, "max_tokens": 2048, "timeout": 600},  # Pod B proposer
    "reviewer":     {"port": 8083, "temp": 0.10, "max_tokens": 400,  "timeout": 480},  # Pod B (legacy)
    "day-verify":{"port": 8082, "temp": 0.0,  "max_tokens": 512,  "timeout": 120},  # Pod B verify
    "day-enricher":{"port": 8082, "temp": 0.1,  "max_tokens": 512,  "timeout": 900},  # Pod B enrich (9B Q8)
    "reflector":    {"port": 8082, "temp": 0.10, "max_tokens": 2048, "timeout": 600},  # Pod B reflector
    "verifier":     {"port": 8084, "temp": 0.10, "max_tokens": 4096, "timeout": 1200}, # Pod B verifier
    "judge":        {"port": 8083, "temp": 0.10, "max_tokens": 4096, "timeout": 7200}, # Pod B judge
    # Non-LLM service endpoints (port-only, for pipeline scripts)
    "reranker":     {"port": 8080},  # Pod A reranker
    "tiny":         {"port": 8080},  # Pod A tiny 0.5B — lightweight idle model
    "embeder":     {"port": 8081},  # Pod B embed mode
    # Role aliases — pipeline code uses these; MODEL_REGISTRY is the single
    # place to change when a model/port changes.
    # Day pipeline — extract (:00/:30)
    "day_extract": {"_model": "extractor"},
    "day_enrich": {"_model": "day-enricher"},  # day enrich pipeline (9B Q4)

    # Day pipeline — verify & rubric
    "day_verify":  {"_model": "day-verify"},

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
        "model": model,
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
        "model": "reranker",
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
        return -1.0  # distinguish error from genuine low score


def reranker_nli_verdict(score: float) -> str:
    """Map reranker relevance score to grounding verdict.

    ⚠ LIMITATION: This reranker (Pod A reranker via llama.cpp --reranking)
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
        <  0       → RERANKER_ERROR (connection/API failure)
        >= 0.75   → GROUNDED  (confident accept — content is relevant to source)
        >= 0.40   → AMBIGUOUS (somewhat related — accepted, downstream catches)
        <  0.40   → UNGROUNDED (confident reject — content unrelated to source)
    """
    if score < 0:
        return "RERANKER_ERROR"
    if score >= 0.75:
        return "GROUNDED"
    elif score >= 0.40:
        return "AMBIGUOUS"
    return "UNGROUNDED"


def _call_nli_server(source: str, evidence: str, strict: bool = False,
                     nli_port: int = 8085, timeout: int = 30) -> str:
    """DEPRECATED — DeBERTa-v3 NLI server on port 8085 (never deployed).

    All production NLI uses LLM self-verify via call_llm_with_retry.
    Retained only for tests/test_verify_methods.py."""
    body = json.dumps({
        "source": source[:4000],
        "evidence": evidence[:1000],
        "strict": strict,
    }).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{nli_port}/nli", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        return data.get("label_3class", "NEUTRAL")
    except Exception as e:
        print(f"  [nli] call failed: {e}", flush=True)
        return "NEUTRAL"


def call_llm_json(
    messages: List[Dict[str, str]],
    model: str = "reviewer",
    **kwargs: Any,
) -> str:
    """Convenience wrapper — same as ``call_llm(…, json_mode=True)``."""
    return call_llm(messages, model, json_mode=True, **kwargs)


def recall_tiny() -> None:
    """Recall tiny model on Pod A to evict heavy model and reduce memory.

    Best-effort — never raises. Call after finishing with Pod A heavy models
    (reranker, polisher) to restore lightweight idle state.
    """
    try:
        messages = [{"role": "user", "content": "ping"}]
        call_llm(messages, model="tiny", max_tokens=2, temperature=0, timeout=15)
    except Exception:
        pass


# ── 8082 Auto-Recovery ──────────────────────────────────────────────
# Used by extract.py, enrich.py, day_verify.py to recover from 8082 crashes.
# Import call_llm_with_retry from this module instead of duplicating logic.

_8082_RECOVERY_LOCK = threading.Lock()

_CONNECTION_ERROR_SUBSTRINGS = (
    "Remote end closed", "Connection reset", "Connection refused",
    "Broken pipe", "RemoteDisconnected",
)


def is_8082_connection_error(e: Exception) -> bool:
    """Check if an exception is a 8082 connection error."""
    err = str(e)
    if "8082" not in err and "extractor" not in err:
        return False
    return any(s in err for s in _CONNECTION_ERROR_SUBSTRINGS)


def recover_8082(model_key: str = "day-extractor") -> None:
    """Reload model on 8082 (thread-safe, only one recovery at a time).

    Args:
        model_key: MODEL_METADATA key for the model to reload (default day-extractor).
                   Must match the current pipeline phase (day-verifier for verify phase).
    """
    if not _8082_RECOVERY_LOCK.acquire(blocking=False):
        print("  [recovery] Another recovery in progress, waiting...", flush=True)
        _8082_RECOVERY_LOCK.acquire(blocking=True)
        print("  [recovery] Recovery finished by other thread", flush=True)
        _8082_RECOVERY_LOCK.release()
        return
    try:
        print(f"  [recovery] Reloading 8082 → {model_key}...", flush=True)
        from lib.pod_manager import ensure_model
        ensure_model(model_key, skip_if_healthy=False)
        print("  [recovery] 8082 ready", flush=True)
    except Exception as recover_err:
        print(f"  [recovery] 8082 reload failed: {recover_err}", flush=True)
    finally:
        _8082_RECOVERY_LOCK.release()


def _model_key_for_8082(model: str) -> str:
    """Map MODEL_REGISTRY role to the correct MODEL_METADATA key for 8082 recovery.

    During day cycle, different phases may be running different models on :8082.
    This returns the correct model key so recovery loads the right model.
    """
    mapping = {
        # day_verify phase → day-verifier (Qwen2.5-Coder-7B)
        "day-verify": "day-verifier",
        # day_extract phase → day-extractor (Qwen3-8B)
        "day_extract": "day-extractor",
        "extractor": "day-extractor",
        # day_enrich phase → day-enricher (Qwen3.5-9B)
        "day-enricher": "day-enricher",
        "day_enrich": "day-enricher",
    }
    return mapping.get(model, "day-extractor")


def call_llm_with_retry(*args, **kwargs):
    """Call call_llm, retry once with 8082 reload on connection error.

    Uses the model name from kwargs to determine which model to reload,
    so the correct phase model is restored (day-verifier for verify phase,
    day-extractor for extract phase, etc.).
    """
    try:
        return call_llm(*args, **kwargs)
    except Exception as e:
        if is_8082_connection_error(e):
            model = kwargs.get("model", "day-extractor")
            model_key = _model_key_for_8082(model)
            print(f"  [recovery] 8082 error ({model}): {type(e).__name__}", flush=True)
            recover_8082(model_key)
            return call_llm(*args, **kwargs)
        raise

