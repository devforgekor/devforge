# Status: production
#!/usr/bin/env python3
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, escape_sql_string, psql_json
from lib.verify import utils, core

def _get_turns_for_verify(limit: int = utils.BATCH_LIMIT) -> List[Dict]:
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, t.created_at::text "
        "FROM turns t "
        "WHERE EXISTS (SELECT 1 FROM review_facts rf WHERE rf.turn_id = t.id AND rf.fact_type IN ('text','user','thinking')) "
        "AND EXISTS (SELECT 1 FROM review_facts rf WHERE rf.turn_id = t.id AND rf.fact_type = 'mcp_meta') "
        "AND NOT EXISTS (SELECT 1 FROM review_facts rf WHERE rf.turn_id = t.id AND rf.fact_type = 'verify_result') "
        "ORDER BY t.created_at ASC LIMIT " + str(limit)
    )
    return psql_json(sql) or []

def _get_turn_extractions(turn_id: str) -> List[Dict]:
    sql = (
        "SELECT fact_type, evidence::text, fact_action "
        "FROM review_facts "
        f"WHERE turn_id = '{escape_sql_string(turn_id)}'::uuid "
        "AND fact_type IN ('text','user','thinking') "
        "ORDER BY fact_index ASC"
    )
    return psql_json(sql) or []

def _insert_verify_result(turn_id: str, fact_index: int, verify_json_str: str, model_label: str, category_summary: str = "") -> bool:
    sql = (
        "INSERT INTO review_facts (turn_id, fact_index, fact_type, evidence, extract_model, verdict, source, fact_action, fact_confidence) "
        f"VALUES ('{escape_sql_string(turn_id)}'::uuid, {fact_index}, 'verify_result', '{escape_sql_string(verify_json_str)}', '{escape_sql_string(model_label)}', 'pending', 'day_verify', '{escape_sql_string(category_summary)}', 100)"
    )
    return psql_ok(sql)

def day_verify_pipeline(limit: int = utils.BATCH_LIMIT, dry_run: bool = False, model_label: str = "14b"):
    print(f"\n[Day Verify] Starting pipeline (model={model_label})")
    turns = _get_turns_for_verify(limit)
    if not turns:
        print("No turns to verify.")
        return

    for turn in turns:
        tid = turn["id"]
        print(f"  Processing {tid[:8]}...")
        extractions = _get_turn_extractions(tid)
        findings = core.build_findings_from_turn(turn, extractions)
        context = core.findings_to_context(findings, turn.get("user_turn"), turn.get("thinking"), turn.get("text"))
        
        result = core.call_verifier(context, model_label=model_label, dry_run=dry_run)
        if not result:
            continue
        
        cat_summary = utils.build_category_summary(result.get("verification_items", []), len(findings))
        
        if not dry_run:
            fi_str = psql(f"SELECT COALESCE(MAX(fact_index), -1) + 1 FROM review_facts WHERE turn_id = '{escape_sql_string(tid)}'::uuid")
            fi = int(fi_str) if fi_str else 0
            _insert_verify_result(tid, fi, json.dumps(result, ensure_ascii=False), model_label, category_summary=cat_summary)
            print(f"    Stored verify_result (fact_index={fi})")
        else:
            print(f"    [DRY] Category Summary: {cat_summary}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=utils.BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--model", default="14b")
    args = parser.parse_args()
    day_verify_pipeline(limit=args.limit, dry_run=args.dry_run, model_label=args.model)
