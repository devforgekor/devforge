# Status: deprecated
# Path: replaced by pipelines/extract.py (Pod B embedder :8081) — only called by deprecated lib/extract/core.py, remove after 2026-07
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from sentence_transformers import SentenceTransformer

# Embedder cache (lazy load)
_EMBEDDER_BGE: Optional[SentenceTransformer] = None
_EMBEDDER_KO: Optional[SentenceTransformer] = None

def _parse_json(raw: str, label: str = "LLM", attempt: int = 1) -> Optional[Dict[str, Any]]:
    from lib.llm.json_parser import parse_llm_json
    return parse_llm_json(raw)

def _get_embedder_bge():
    global _EMBEDDER_BGE
    if _EMBEDDER_BGE is None:
        _EMBEDDER_BGE = SentenceTransformer("BAAI/bge-m3")
    return _EMBEDDER_BGE

def _get_embedder_ko():
    global _EMBEDDER_KO
    if _EMBEDDER_KO is None:
        _EMBEDDER_KO = SentenceTransformer("jhgan/ko-sroberta-multitask")
    return _EMBEDDER_KO

def _batch_cosine(embedder: SentenceTransformer, ev_list: List[str], src_list: List[str]):
    import torch.nn.functional as F
    import torch
    ev_emb = embedder.encode(ev_list, convert_to_tensor=True)
    src_emb = embedder.encode(src_list, convert_to_tensor=True)
    cos_sim = F.cosine_similarity(ev_emb, src_emb)
    return cos_sim.tolist()

def _clean_markdown(text: str) -> str:
    if not text: return ""
    text = re.sub(r'#+\s+', '', text)
    text = re.sub(r'\*\*|__', '', text)
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'`', '', text)
    return text.strip()

def _infer_category(evidence: str, current_cat: str) -> str:
    ev_low = evidence.lower()
    if any(k in ev_low for k in ["에러", "오류", "error", "fail", "bug"]): return "ISSUE"
    if any(k in ev_low for k in ["설정", "config", "env", "셋업"]): return "CONFIG"
    if any(k in ev_low for k in ["성능", "속도", "latency", "perf"]): return "PERF"
    return current_cat

def _sample_content(path: str, max_bytes: int = 2048) -> str:
    try:
        if not os.path.exists(path): return ""
        with open(path, "rb") as f:
            chunk = f.read(max_bytes)
            return chunk.decode("utf-8", errors="ignore")
    except Exception:
        return ""
