# Status: production
import json
import time
import os
from typing import Any, Dict, List, Optional
from sentence_transformers import SentenceTransformer
from minicheck.minicheck import MiniCheck
from lib.llm_client import call_llm
from lib.common import strip_think
from lib.llm.json_parser import save_dlq, parse_llm_json
from lib.token_budget import TokenBudget
from lib.mcp import utils

# Lazy singletons
_EMBEDDER = None
_NLI_MODEL = None

def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = SentenceTransformer('all-MiniLM-L6-v2')
    return _EMBEDDER

def get_nli():
    global _NLI_MODEL
    if _NLI_MODEL is None:
        _NLI_MODEL = MiniCheck(model_name="flan-t5-large", cache_dir="/opt/ai_data/models")
    return _NLI_MODEL

def parse_mcp_json(raw: str, label: str = "MCP", attempt: int = 1) -> Optional[Dict[str, Any]]:
    cleaned = strip_think(raw)
    result = parse_llm_json(cleaned)
    if result is None:
        save_dlq(raw, stage=f"mcp_{label}", error="parse_llm_json returned None",
                 attempt=attempt)
    return result

def entity_cosine_grounding(entities: Dict, turn_texts: List[str]) -> Dict:
    try:
        emb = get_embedder()
    except Exception:
        return {}
    result = {}
    for cat in ("technologies", "functions", "files", "mentioned_users"):
        items = entities.get(cat, []) if isinstance(entities, dict) else []
        if not items or not isinstance(items, list):
            result[cat] = []
            continue
        if not turn_texts:
            result[cat] = [{"entity": i, "cosine": 0.0, "relevant": False} for i in items[:5]]
            continue
        try:
            item_vecs = emb.encode(items[:5], normalize_embeddings=True)
            text_vecs = emb.encode(turn_texts, normalize_embeddings=True)
            sims = (item_vecs @ text_vecs.T).max(axis=1)
            result[cat] = [
                {"entity": items[i], "cosine": round(float(sims[i]), 3),
                 "relevant": float(sims[i]) >= utils.COSINE_ENTITY_RELEVANCE}
                for i in range(len(items[:5]))
            ]
        except Exception:
            result[cat] = []
    return result

def entity_nli_grounding(entities: Dict, turn_text: str) -> Dict:
    try:
        nli = get_nli()
    except Exception:
        return {}
    candidates = []
    for cat in ("technologies", "functions", "files", "mentioned_users"):
        items = entities.get(cat, []) if isinstance(entities, dict) else []
        if items and isinstance(items, list):
            for i in items[:3]:
                candidates.append((cat, str(i).strip()))
            if len(candidates) >= 5:
                break
    candidates = candidates[:5]
    if not candidates or not turn_text:
        return {}

    claims = [c[1] for c in candidates]
    try:
        label, prob, _, _ = nli.score(docs=[turn_text] * len(claims), claims=claims)
        result = {}
        for (cat, ent), lbl, pr in zip(candidates, label, prob):
            result.setdefault(cat, []).append({
                "entity": ent,
                "supported": bool(lbl == 1 and pr >= utils.MINICHECK_SUPPORT),
                "prob": round(float(pr), 3),
            })
        return result
    except Exception:
        return {}

def tldr_cosine_quality(tldr: str, turn_text: str) -> float:
    if not tldr or not turn_text:
        return 0.0
    try:
        emb = get_embedder()
    except Exception:
        return 0.0
    try:
        tldr_vec = emb.encode(tldr, normalize_embeddings=True)
        text_vec = emb.encode(turn_text[:500], normalize_embeddings=True)
        return round(float(tldr_vec @ text_vec), 3)
    except Exception:
        return 0.0

def generate_mcp_fields(user_turn: str, thinking: str, text: str,
                         system_prompt: str,
                         model: str = "day_mcp",
                         extractions: Optional[List[Dict]] = None,
                         dry_run: bool = False
                         ) -> Optional[Dict[str, Any]]:
    if dry_run:
        print(f"    [DRY] Mocking LLM call for {model}")
        return {
            "tldr": "Mock summary for dry run",
            "intent": "other",
            "category": "other",
            "entities": {"files": [], "technologies": [], "functions": [], "mentioned_users": []},
            "tags": ["mock"],
            "_meta": {"usage": {"prompt_tokens": 0, "completion_tokens": 0}, "elapsed_ms": 0.0, "model": model}
        }

    budget = TokenBudget("mcp_enrich")
    parts = ["=== user_turn ==="]
    if budget.add_section("user_turn", user_turn or "(empty)", priority=10):
        parts.append(user_turn or "(empty)")

    parts.append("")
    parts.append("=== thinking ===")
    if budget.add_section("thinking", thinking or "(empty)", priority=4):
        parts.append(thinking or "(empty)")

    parts.append("")
    parts.append("=== text ===")
    if budget.add_section("text", text or "(empty)", priority=7):
        parts.append(text or "(empty)")

    if extractions:
        parts.append("")
        parts.append("=== extracted facts ===")
        for idx, ex in enumerate(extractions):
            confidence = ex.get('fact_confidence', 100)
            line = f"  [{ex.get('fact_type','?')}] (conf={confidence}) {ex.get('evidence','')[:300]}"
            if budget.add_section(f"extract_{idx}", line, priority=6):
                parts.append(line)
    parts.append("")
    if budget.used > 0:
        parts.append(f"[context budget: {budget.used}/{budget.limit} tok]")

    meta = call_llm(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": "\n".join(parts)}],
        model=model,
        max_tokens=utils.MAX_TOKENS_MCP, temperature=utils.TEMP_MCP, timeout=utils.TIMEOUT_MCP,
        json_mode=True, return_meta=True,
    )
    result = parse_mcp_json(meta["content"], "MCP fields")
    if result:
        result["_meta"] = {"usage": meta["usage"], "timings": meta["timings"],
                           "elapsed_ms": meta["elapsed_ms"], "model": model}
    return result
