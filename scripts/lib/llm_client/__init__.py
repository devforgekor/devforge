#!/usr/bin/env python3
# Status: production
"""Unified LLM client — single entry point for all pipeline scripts."""

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from lib.llm_client.feedback import _inject_feedback
from lib.llm_client.recovery import _model_key_for_8082, is_8082_connection_error, recover_8082

MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "extractor": {"port": 8082, "temp": 0.12, "max_tokens": 2048, "timeout": 300},
    "cleaner": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 600},
    "proposer": {"port": 8081, "temp": 0.22, "max_tokens": 2048, "timeout": 600},
    "reviewer": {"port": 8083, "temp": 0.10, "max_tokens": 400, "timeout": 480},
    "day-verify": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-enricher": {"port": 8082, "temp": 0.1, "max_tokens": 512, "timeout": 900},
    "reflector": {"port": 8082, "temp": 0.10, "max_tokens": 2048, "timeout": 600},
    "verifier": {"port": 8084, "temp": 0.10, "max_tokens": 4096, "timeout": 1200},
    "judge": {"port": 8083, "temp": 0.10, "max_tokens": 4096, "timeout": 7200},
    "reranker": {"port": 8080},
    "tiny": {"port": 8080},
    "embeder": {"port": 8081},
    "day_extract": {"_model": "extractor"},
    "day_enrich": {"_model": "day-enricher"},
    "day_verify": {"_model": "day-verify"},
    "day_proposer": {"_model": "reviewer"},
    "day_reviewer": {"_model": "reviewer"},
    "day_judge": {"_model": "reviewer"},
    "night_proposer": {"_model": "proposer"},
    "night_reflector": {"_model": "reflector"},
    "night_judge": {"_model": "judge"},
    "night_verify": {"_model": "verifier"},
}


def resolve_model(name: str) -> str:
    cfg = MODEL_REGISTRY.get(name)
    return cfg["_model"] if cfg and "_model" in cfg else name


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
    _http_timeout = min(timeout or cfg["timeout"], 1800)
    print(
        f"  [call_llm] {model}:{port} timeout={_http_timeout}s max_tokens={body['max_tokens']}",
        flush=True,
    )
    try:
        with urllib.request.urlopen(req, timeout=_http_timeout) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM call to :{port} ({model}) failed: {e}")
    except socket.timeout:
        raise RuntimeError(f"LLM call to :{port} ({model}) timed out after {_http_timeout}s")
    elapsed_ms = (time.monotonic() - t_start) * 1000

    choices = result.get("choices", [])
    if not choices:
        raise RuntimeError(f"LLM ({model}) returned no choices: {result}")
    content = (choices[0]["message"].get("content") or "").strip()
    usage = result.get("usage", {})
    timings = result.get("timings", {})

    try:
        from lib.watchdog.messenger import heartbeat

        heartbeat(f"llm_{model}", detail=f"ok:{elapsed_ms:.0f}ms")
    except Exception:
        pass

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
    reranker_port = MODEL_REGISTRY["reranker"]["port"]
    tr = lambda s: s[:2000] if isinstance(s, str) else str(s)[:2000]
    body = json.dumps(
        {
            "model": "reranker",
            "query": tr(query),
            "documents": [tr(document)],
            "top_n": 1,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{reranker_port}/v1/rerank",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode())
        return float(data["results"][0]["relevance_score"])
    except Exception as e:
        print(f"  [reranker] score call failed: {e}", flush=True)
        return -1.0


def reranker_nli_verdict(score: float) -> str:
    if score < 0:
        return "RERANKER_ERROR"
    if score >= 0.75:
        return "GROUNDED"
    elif score >= 0.40:
        return "AMBIGUOUS"
    return "UNGROUNDED"


def _call_nli_server(
    source: str, evidence: str, strict: bool = False, nli_port: int = 8085, timeout: int = 30
) -> str:
    body = json.dumps(
        {
            "source": source[:4000],
            "evidence": evidence[:1000],
            "strict": strict,
        }
    ).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{nli_port}/nli",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
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
    return call_llm(messages, model, json_mode=True, **kwargs)


def recall_tiny() -> None:
    try:
        messages = [{"role": "user", "content": "ping"}]
        call_llm(messages, model="tiny", max_tokens=2, temperature=0, timeout=15)
    except Exception:
        pass


def call_llm_with_retry(*args, **kwargs):
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
