"""Local LLM adapter — Track A (llama.cpp HTTP servers only).

Implements LLMPort for local llama.cpp servers running on ports 8080-8085.
This is the PRODUCTION adapter for the current single-server setup.

Track B (cloud provider support) would add a separate adapter that
implements the same LLMPort interface using OpenAI/Anthropic SDKs.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from devforge.core.config import get_config
from devforge.core.logging import get_logger
from devforge.ports.extract import ExtractedFact, LLMPort, TurnData

logger = get_logger(__name__)

# ── Model registry — mirrors scripts/lib/llm_client/__init__.py ──
MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "extractor": {"port": 8082, "temp": 0.12, "max_tokens": 2048, "timeout": 300},
    "extractor-b": {"port": 8083, "temp": 0.0, "max_tokens": 1024, "timeout": 300},
    "cleaner": {"port": 8080, "temp": 0.0, "max_tokens": 512, "timeout": 600},
    "proposer": {"port": 8081, "temp": 0.22, "max_tokens": 2048, "timeout": 600},
    "reviewer": {"port": 8083, "temp": 0.10, "max_tokens": 400, "timeout": 480},
    "day-verify": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-b": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q8": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q8-b": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q4": {"port": 8082, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-verify-q4-b": {"port": 8083, "temp": 0.0, "max_tokens": 512, "timeout": 120},
    "day-enricher": {"port": 8082, "temp": 0.1, "max_tokens": 512, "timeout": 900},
    "day-enricher-b": {"port": 8083, "temp": 0.1, "max_tokens": 512, "timeout": 900},
    "reflector": {"port": 8082, "temp": 0.10, "max_tokens": 2048, "timeout": 600},
    "verifier": {"port": 8084, "temp": 0.10, "max_tokens": 4096, "timeout": 1200},
    "judge": {"port": 8083, "temp": 0.10, "max_tokens": 4096, "timeout": 7200},
    "reranker": {"port": 8080},
    "tiny": {"port": 8080},
    "embeder": {"port": 8081},
    # Day-mode aliases
    "day_extract": {"_model": "extractor"},
    "day_extract_b": {"_model": "extractor-b"},
    "day_enrich": {"_model": "day-enricher"},
    "day_enrich_b": {"_model": "day-enricher-b"},
    "day_verify": {"_model": "day-verify"},
    "day_verify_b": {"_model": "day-verify-b"},
    "day_verify_q8": {"_model": "day-verify-q8"},
    "day_verify_q8_b": {"_model": "day-verify-q8-b"},
    "day_verify_q4": {"_model": "day-verify-q4"},
    "day_verify_q4_b": {"_model": "day-verify-q4-b"},
    "day_proposer": {"_model": "reviewer"},
    "day_reviewer": {"_model": "reviewer"},
    "day_judge": {"_model": "reviewer"},
    # Night-mode aliases
    "night_proposer": {"_model": "proposer"},
    "night_reflector": {"_model": "reflector"},
    "night_judge": {"_model": "judge"},
    "night_verify": {"_model": "verifier"},
}


def resolve_model(name: str) -> str:
    """Resolve a day/night alias to a concrete model name."""
    cfg = MODEL_REGISTRY.get(name)
    if not cfg:
        raise ValueError(f"Unknown model: {name}. Known: {list(MODEL_REGISTRY)}")
    return cfg["_model"] if "_model" in cfg else name


class LocalLLMAdapter(LLMPort):
    """Track A: llama.cpp HTTP servers on localhost ports 8080-8085.

    All LLM calls go through HTTP to local llama.cpp servers.
    No external API keys required (Track A only).
    """

    def __init__(self, model_registry: Optional[dict] = None):
        self._registry = model_registry or MODEL_REGISTRY
        self._config = get_config()

    def _resolve(self, model_key: str) -> tuple[str, dict[str, Any]]:
        """Resolve model_key to (model_name, config_dict)."""
        cfg = self._registry.get(model_key)
        if not cfg:
            raise ValueError(f"Unknown model: {model_key}. Known: {list(self._registry)}")
        if "_model" in cfg:
            model_key = cfg["_model"]
            cfg = self._registry[model_key]
        return model_key, cfg

    async def _call_llm(
        self,
        messages: list[dict[str, str]],
        model_key: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        timeout: Optional[int] = None,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        """HTTP call to local llama.cpp server.

        Uses synchronous urllib under asyncio.to_thread for compatibility
        with the existing sync codebase. Future migration can use httpx/async.
        """
        model_name, cfg = self._resolve(model_key)
        port = cfg["port"]
        body: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else cfg.get("max_tokens", 2048),
            "temperature": temperature if temperature is not None else cfg.get("temp", 0.12),
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        req = Request(url, data=data, headers={"Content-Type": "application/json"})
        http_timeout = min(timeout or cfg.get("timeout", 300), 1800)

        logger.debug("llm_call", model=model_name, port=port,
                      max_tokens=body["max_tokens"], timeout=http_timeout)

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: urlopen(req, timeout=http_timeout).read().decode("utf-8"),
            )
            response = json.loads(result)
        except (URLError, socket.timeout, json.JSONDecodeError) as e:
            logger.error("llm_call_failed", model=model_name, port=port, error=str(e))
            raise RuntimeError(f"LLM call to :{port} ({model_name}) failed: {e}")

        choices = response.get("choices", [])
        if not choices:
            raise RuntimeError(f"LLM ({model_name}) returned no choices: {response}")

        content = (choices[0]["message"].get("content") or "").strip()
        usage = response.get("usage", {})
        elapsed_ms = 0  # Would need timing in the actual call

        return {
            "content": content,
            "usage": usage,
            "elapsed_ms": elapsed_ms,
            "model": model_name,
        }

    # ── LLMPort implementation ──

    async def extract_facts(
        self,
        turn: TurnData,
        model_key: str = "day_extract",
        max_tokens: Optional[int] = None,
    ) -> list[ExtractedFact]:
        """Extract structured facts from a turn via LLM.

        This is a simplified interface — the full extract logic with
        section-major extraction, JSON recovery, and EDC normalization
        lives in the pipeline stage implementation. The adapter just
        handles the LLM HTTP call.
        """
        from devforge.pipeline_stages.extract_edc import build_extract_prompt, parse_extract_response

        messages = build_extract_prompt(turn)
        result = await self._call_llm(
            messages, model_key,
            max_tokens=max_tokens,
            json_mode=True,
        )

        facts = parse_extract_response(result["content"], turn.id, model_key)
        return facts

    async def verify_claim(
        self,
        claim: str,
        evidence: str,
        model_key: str = "day_verify",
    ) -> dict[str, Any]:
        """NLI verification — check if claim is grounded by evidence."""
        model_name, cfg = self._resolve(model_key)
        port = cfg.get("nli_port", 8085)

        body = json.dumps({
            "source": claim[:4000],
            "evidence": evidence[:1000],
            "strict": False,
        }).encode()

        url = f"http://127.0.0.1:{port}/nli"
        req = Request(url, data=body,
                      headers={"Content-Type": "application/json"}, method="POST")

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None,
                lambda: json.loads(urlopen(req, timeout=30).read().decode()),
            )
            verdict = data.get("label_3class", "NEUTRAL")
        except Exception as e:
            logger.warning("nli_verify_failed", error=str(e))
            verdict = "NEUTRAL"

        return {"verdict": verdict, "claim": claim, "evidence": evidence}

    async def enrich_fact(
        self,
        fact: ExtractedFact,
        model_key: str = "day_enrich",
        max_tokens: Optional[int] = None,
    ) -> dict[str, Any]:
        """Enrich a fact with metadata context."""
        # Build enrichment prompt
        messages = [
            {"role": "system", "content": "Enrich the extracted facts with context."},
            {"role": "user", "content": fact.evidence[:2000]},
        ]

        result = await self._call_llm(
            messages, model_key,
            max_tokens=max_tokens or 512,
            temperature=0.1,
        )

        return {
            "enriched_text": result["content"],
            "model": result["model"],
            "usage": result["usage"],
            "elapsed_ms": result["elapsed_ms"],
        }

    async def rerank(
        self,
        query: str,
        candidates: list[str],
    ) -> list[float]:
        """Rerank search candidates by relevance to query."""
        cfg = MODEL_REGISTRY["reranker"]
        port = cfg["port"]

        body = json.dumps({
            "model": "reranker",
            "query": query[:1500],
            "documents": [c[:1500] for c in candidates],
            "top_n": len(candidates),
        }).encode()

        url = f"http://127.0.0.1:{port}/v1/rerank"
        req = Request(url, data=body,
                      headers={"Content-Type": "application/json"}, method="POST")

        try:
            import asyncio
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(
                None,
                lambda: json.loads(urlopen(req, timeout=120).read().decode()),
            )
            scores = [float(r["relevance_score"]) for r in data.get("results", [])]
        except Exception as e:
            logger.warning("rerank_failed", error=str(e))
            scores = [-1.0] * len(candidates)

        return scores


# ── Re-export MODEL_REGISTRY for backwards compatibility ──
__all__ = ["LocalLLMAdapter", "MODEL_REGISTRY", "resolve_model"]
