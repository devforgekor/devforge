#!/usr/bin/env python3
# Status: experimental
# Path: imported by — scripts/proxies/unified_search.py
"""Relevance reranking for search results.

[WHY] 검색 결과 순서는 제공자마다 다르고 항상 최적이 아니다. 표준(TREC/RAG)은
후보를 reranker로 재정렬해 상위 품질을 올린다. 우선순위:
  1) OpenRouter 무료 리랭커(`~/.config/devforge/rerank_models.json`, :free만)
  2) 로컬 리랭커(127.0.0.1:8080 /v1/rerank)
  3) 원본 순서(폴백)
크레딧 소진 정책: 유료 모델은 후보 파일에 기록되지 않으므로 여기서도 사용하지 않는다.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

RERANK_CONFIG = Path.home() / ".config/devforge/rerank_models.json"
LOCAL_RERANK_URL = "http://127.0.0.1:8080/v1/rerank"
LOCAL_MODEL = "reranker"
DEFAULT_TIMEOUT = 25
_MAX_DOC_CHARS = 1500


def _load_config() -> dict:
    try:
        return json.loads(RERANK_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _or_keys() -> list[str]:
    keys = [
        os.environ.get(k, "")
        for k in (
            "OPENROUTER_API_KEY",
            "OPENROUTER_MESIDS_API_KEY",
            "OPENROUTER_MINIPARK4U_API_KEY",
            "OPENROUTER_HYEONMINPARK4U_API_KEY",
        )
    ]
    return [k for k in keys if k.startswith("sk-or")]


def _call_rerank(url: str, body: dict, headers: dict, timeout: int) -> Optional[list[dict]]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        results = data.get("results") or data.get("data") or []
        return results if isinstance(results, list) else None
    except urllib.error.HTTPError as e:
        logger.debug(f"rerank HTTP {e.code} ({url})")
        return None
    except Exception as e:  # noqa: BLE001 — 재정렬 실패는 치명적이지 않다
        logger.debug(f"rerank failed ({url}): {type(e).__name__}")
        return None


def _rerank_openrouter(query: str, docs: list[str], cfg: dict) -> Optional[list[int]]:
    """OpenRouter :free 리랭커로 순위 반환(index 리스트, 상위부터)."""
    chain = [cfg.get("primary")] + list(cfg.get("chain") or [])
    chain = [m for m in dict.fromkeys(chain) if m and str(m).endswith(":free")]
    keys = _or_keys()
    if not chain or not keys:
        return None
    for i, model in enumerate(chain):
        for attempt in range(2):  # :free 업스트림은 간헐 502 → 재시도
            key = keys[(i + attempt) % len(keys)]
            res = _call_rerank(
                "https://openrouter.ai/api/v1/rerank",
                {"model": model, "query": query, "documents": docs, "top_n": len(docs)},
                {"Authorization": f"Bearer {key}", "User-Agent": "devforge-search/1.0"},
                DEFAULT_TIMEOUT,
            )
            if res:
                return [r.get("index") for r in res if isinstance(r.get("index"), int)]
    return None


def _rerank_local(query: str, docs: list[str]) -> Optional[list[int]]:
    """로컬 리랭커(8080)로 순위 반환."""
    res = _call_rerank(
        LOCAL_RERANK_URL,
        {"model": LOCAL_MODEL, "query": query, "documents": docs, "top_n": len(docs)},
        {},
        DEFAULT_TIMEOUT,
    )
    if not res:
        return None
    return [r.get("index") for r in res if isinstance(r.get("index"), int)]


def rerank_indices(query: str, docs: list[str]) -> Optional[list[int]]:
    """문서 리스트의 재정렬 순서(index) 반환. 실패 시 None(원본 유지)."""
    if len(docs) < 2:
        return None
    docs = [d[:_MAX_DOC_CHARS] for d in docs]
    idx = _rerank_openrouter(query, docs, _load_config())
    if idx:
        return idx
    return _rerank_local(query, docs)


def rerank_records(query: str, records: list[dict], text_key: str = "description") -> list[dict]:
    """구조화 결과(dict)를 재정렬. 실패하면 원본 순서 그대로."""
    texts = [str(r.get(text_key) or r.get("title") or "") for r in records]
    idx = rerank_indices(query, texts)
    if not idx:
        return records
    seen = set()
    ordered = []
    for i in idx:
        if 0 <= i < len(records) and i not in seen:
            seen.add(i)
            ordered.append(records[i])
    ordered.extend(r for i, r in enumerate(records) if i not in seen)  # 누락분 보존
    return ordered
