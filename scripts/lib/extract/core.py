# Status: deprecated
# Path: replaced by pipelines/extract.py (Pod B embedder :8081 + LLM self-verify + reranker) — bge-m3/ko-sroberta removed, remove after 2026-07
import os
import sys
import time
from typing import Any, Dict, List, Optional
from lib.extract import utils
from lib.db import psql, psql_ok, escape_sql_string, psql_json, get_checkpoint
from lib.llm_client import call_llm
from lib.common import log, strip_think

def _check_faithfulness_scored(evidence: str, source: str) -> dict:
    bge = utils._get_embedder_bge()
    ko = utils._get_embedder_ko()

    ev_clean = utils._clean_markdown(evidence)
    src_clean = utils._clean_markdown(source)

    bge_score = utils._batch_cosine(bge, [ev_clean], [src_clean])[0]
    ko_score = utils._batch_cosine(ko, [ev_clean], [src_clean])[0]

    return {
        "bge": bge_score,
        "ko": ko_score,
        "verdict": _dual_embedding_verdict(bge_score, ko_score, ev_clean, src_clean)
    }

def _dual_embedding_verdict(bge_cos: float, ko_cos: float, evidence: str, source: str) -> str:
    if bge_cos > 0.85 or ko_cos > 0.85: return "HIGH"
    if bge_cos > 0.70 and ko_cos > 0.70: return "MID"
    if bge_cos < 0.40: return "LOW"
    return "REVIEW"

def _extract_facts(user_turn: str, thinking: str, text: str) -> List[Dict[str, Any]]:
    prompt = f"Extract facts from:\nUser: {user_turn}\nAssistant: {text}"
    resp = call_llm("day_extract", prompt)
    return utils._parse_json(resp) or []

def _verify_extractions(extractions: List[Dict[str, Any]], user_turn: str, thinking: str, text: str) -> List[Dict[str, Any]]:
    source_context = f"{user_turn}\n{thinking}\n{text}"
    verified = []
    for ext in extractions:
        score_info = _check_faithfulness_scored(ext.get("evidence", ""), source_context)
        ext["faithful_score"] = int(max(score_info["bge"], score_info["ko"]) * 100)
        ext["faithful_verdict"] = score_info["verdict"]
        verified.append(ext)
    return verified

def _insert_fact(turn_id: str, fact_index: int, fact_type: str, evidence: str, extract_model: str, **kwargs) -> bool:
    cols = ["turn_id", "fact_index", "fact_type", "evidence", "extract_model"]
    vals = [f"'{escape_sql_string(str(turn_id))}'", str(fact_index), f"'{escape_sql_string(fact_type)}'", f"'{escape_sql_string(evidence)}'", f"'{escape_sql_string(extract_model)}'"]

    for k, v in kwargs.items():
        if v is not None:
            cols.append(k)
            vals.append(f"'{escape_sql_string(str(v))}'" if isinstance(v, str) else str(v))

    sql = f"INSERT INTO review_facts ({', '.join(cols)}) VALUES ({', '.join(vals)}) ON CONFLICT DO NOTHING"
    return psql_ok(sql)

def _get_unprocessed_turns(limit: int = 50) -> List[Dict[str, Any]]:
    cp = get_checkpoint("extract")
    sql = f"SELECT id, user_turn, thinking, text FROM turns WHERE created_at > '{cp}' ORDER BY created_at ASC LIMIT {limit}"
    return psql_json(sql)
