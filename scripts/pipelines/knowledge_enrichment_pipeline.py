# Status: production
#!/usr/bin/env python3
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

os.environ["TOKENIZERS_PARALLELISM"] = "false"

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, escape_sql_string, psql_json, get_checkpoint, advance_checkpoint
from lib.mcp import utils, core

SYSTEM_DAY_MCP = """\
You are a conversation analyst preparing structured metadata for an MCP
(Model Context Protocol) system. Given the original conversation turn and
the extracted facts, produce structured MCP fields.

Output STRICT JSON:
{
  "tldr": "One-line summary (max 15 words) — what this turn is about",
  "intent": "question|request|report|clarification|code_change|debug|design|other",
  "category": "requirement|decision|explanation|code|reasoning|other",
  "entities": {
    "files": ["relative/file/path.py"],
    "technologies": ["Python", "FastAPI", ...],
    "functions": ["function_name"],
    "mentioned_users": []
  },
  "tags": ["tag1", "tag2"]
}

Rules:
- tldr must be factual and directly derivable from the turn content
- intent must be one of the enumerated values
- category: classify the turn's primary nature
- entities.files: only include file paths explicitly mentioned in the turn
- entities.technologies: programming languages, frameworks, tools mentioned
- entities.functions: function/class/method names mentioned
- tags: 2-5 keywords for discovery and routing
- Use the extracted facts section to inform category and entity accuracy
- If a field has no relevant data, use an empty array []"""

def _get_turns_without_mcp(limit: int = utils.BATCH_LIMIT) -> List[Dict[str, Any]]:
    checkpoint = get_checkpoint("mcp_enrich")
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, t.created_at "
        "FROM turns t "
        "WHERE t.created_at > COALESCE("
        f"  (SELECT max_created_at FROM pipeline_checkpoint WHERE phase = 'mcp_enrich'), "
        "  '-infinity'::timestamptz) "
        "AND EXISTS ("
        "  SELECT 1 FROM review_facts rf "
        "  WHERE rf.turn_id = t.id "
        "  AND rf.fact_type IN ('user','thinking','text')"
        ")"
        "AND NOT EXISTS ("
        "  SELECT 1 FROM review_facts rf2 "
        "  WHERE rf2.turn_id = t.id AND rf2.fact_type = 'mcp_meta'"
        ")"
        "ORDER BY t.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    return [{
        "id": r.get("id", ""),
        "user_turn": r.get("user_turn", ""),
        "thinking": r.get("thinking") or None,
        "text": r.get("text", ""),
        "created_at": r.get("created_at", ""),
    } for r in rows]

def _get_turn_extractions(turn_id: str) -> List[Dict[str, Any]]:
    sql = (
        f"SELECT fact_type, evidence, fact_confidence "
        f"FROM review_facts "
        f"WHERE turn_id = '{escape_sql_string(turn_id)}'::uuid "
        f"  AND fact_type IN ('user','thinking','text') "
        f"ORDER BY fact_index ASC"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    return [{
        "fact_type": r.get("fact_type", "text"),
        "evidence": r.get("evidence", ""),
        "fact_confidence": r.get("fact_confidence", 100),
    } for r in rows]

def _insert_mcp_fact(turn_id: str, fact_index: int,
                     mcp_json_str: str, model: str,
                     prompt_tokens: Optional[int] = None,
                     gen_tokens: Optional[int] = None,
                     elapsed_ms: Optional[float] = None,
                     source_file: Optional[str] = None) -> bool:
    cols = ["turn_id", "fact_index", "fact_type", "evidence", "extract_model",
            "verdict", "source", "fact_action", "fact_confidence"]
    vals = [
        f"'{escape_sql_string(turn_id)}'::uuid",
        str(fact_index),
        "'mcp_meta'",
        f"'{escape_sql_string(mcp_json_str[:5000])}'",
        f"'{escape_sql_string(model)}'",
        "'pending'",
        "'mcp_enrich'",
        "'mcp'",
        "100",
    ]
    set_clauses = []

    if prompt_tokens is not None:
        cols.append("prompt_tokens"); vals.append(str(prompt_tokens))
        set_clauses.append(f"prompt_tokens = {prompt_tokens}")
    if gen_tokens is not None:
        cols.append("gen_tokens"); vals.append(str(gen_tokens))
        set_clauses.append(f"gen_tokens = {gen_tokens}")
    if elapsed_ms is not None:
        cols.append("elapsed_ms"); vals.append(f"{elapsed_ms:.1f}")
        set_clauses.append(f"elapsed_ms = {elapsed_ms:.1f}")
    if source_file:
        cols.append("source_file"); vals.append(f"'{escape_sql_string(source_file)}'")
        set_clauses.append(f"source_file = '{escape_sql_string(source_file)}'")

    sql = (
        f"INSERT INTO review_facts ({', '.join(cols)}) "
        f"VALUES ({', '.join(vals)}) "
        f"ON CONFLICT (turn_id, fact_index, extract_model) "
        f"DO UPDATE SET evidence = EXCLUDED.evidence"
        + (f", {', '.join(set_clauses)}" if set_clauses else "")
    )
    return psql_ok(sql)

def mcp_enrich_pipeline(turn_id: Optional[str] = None,
                        limit: int = utils.BATCH_LIMIT,
                        dry_run: bool = False,
                        model: str = "day_mcp") -> Dict[str, Any]:
    t_start = time.monotonic()
    print(f"\n[MCP Enrich] Starting pipeline (model={model})")

    if turn_id:
        sql = f"SELECT id, user_turn, thinking, text, created_at FROM turns WHERE id = '{escape_sql_string(turn_id)}'::uuid"
        rows = psql_json(sql)
        turns = [{ "id": r["id"], "user_turn": r.get("user_turn",""), "thinking": r.get("thinking"), "text": r.get("text",""), "created_at": r.get("created_at","") } for r in rows] if rows else []
    else:
        turns = _get_turns_without_mcp(limit)

    if not turns:
        print("[mcp_enrich] No turns found.")
        return {"processed": 0, "ok": True}

    processed = 0
    max_created = None

    for ti, turn in enumerate(turns, 1):
        tid = turn["id"]
        ut, th, tx = turn.get("user_turn",""), turn.get("thinking",""), turn.get("text","")
        created = turn.get("created_at")

        print(f"  [{ti}/{len(turns)}] Processing {tid[:8]}...")
        
        extractions = _get_turn_extractions(tid)
        mcp_result = core.generate_mcp_fields(ut, th, tx, SYSTEM_DAY_MCP, model=model, extractions=extractions, dry_run=dry_run)
        if not mcp_result:
            continue

        mcp_result = utils.post_process_mcp(mcp_result, ut, tx)
        
        if mcp_result.get("entities"):
            mcp_result["verified"] = utils.verify_entities(mcp_result)
        
        turn_texts = [t for t in (ut, th, tx) if t]
        if mcp_result.get("entities") and turn_texts:
            mcp_result["cosine_grounding"] = core.entity_cosine_grounding(mcp_result["entities"], turn_texts[:3])
            mcp_result["nli_grounding"] = core.entity_nli_grounding(mcp_result["entities"], "\n".join(turn_texts[:2])[:2000])

        if mcp_result.get("tldr") and turn_texts:
            mcp_result["tldr_cosine"] = core.tldr_cosine_quality(mcp_result["tldr"], turn_texts[0])

        if dry_run:
            processed += 1
            continue

        fi_str = psql(f"SELECT COALESCE(MAX(fact_index), -1) + 1 FROM review_facts WHERE turn_id = '{escape_sql_string(tid)}'::uuid")
        fi = int(fi_str) if fi_str else 0

        mcp_meta = mcp_result.pop("_meta", {})
        _insert_mcp_fact(tid, fi, json.dumps(mcp_result, ensure_ascii=False), model,
                         prompt_tokens=mcp_meta.get("usage",{}).get("prompt_tokens"),
                         gen_tokens=mcp_meta.get("usage",{}).get("completion_tokens"),
                         elapsed_ms=mcp_meta.get("elapsed_ms"))
        processed += 1
        if created and (max_created is None or created > max_created):
            max_created = created

    if not dry_run and max_created:
        advance_checkpoint("mcp_enrich", max_created)

    return {"processed": processed, "ok": True}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--turn-id")
    parser.add_argument("--limit", type=int, default=utils.BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="day_mcp")
    args = parser.parse_args()
    mcp_enrich_pipeline(turn_id=args.turn_id, limit=args.limit, dry_run=args.dry_run, model=args.model)
