# Status: production
#!/usr/bin/env python3
import json, os, sys, argparse, urllib.request
from lib.worklog import utils, core
from lib.db import escape_sql_string, psql_json, psql_ok
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm_json
from lib.infra.preflight import preflight_checks

def fetch_unlogged_turns(date_str: str) -> dict:
    rows = psql_json(f"SELECT id, agent, conversation_id, user_turn, text, created_at FROM turns WHERE created_at::date = '{date_str}'::date AND length(user_turn) > 30 ORDER BY agent, created_at")
    grouped = {}
    for r in rows or []:
        grouped.setdefault(r["agent"] or "unknown", []).append(r)
    return grouped

def check_endpoint(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as resp:
            return resp.status == 200
    except: return False

def run(date_str: str = None, dry_run: bool = False) -> int:
    date_str = date_str or utils.today_kst()
    grouped = fetch_unlogged_turns(date_str)
    total = 0
    for agent, turns in grouped.items():
        if len(turns) < 3: continue
        print(f"Agent {agent}: found {len(turns)} turns")
        if dry_run:
            total += 1
            continue
            
        batch = turns[:utils.BATCH_SIZE]
        prompt = f"Agent: {agent}\nTurns:\n" + "\n".join([f"[{t['created_at']}] {t['user_turn'][:200]}" for t in batch])
        # call_llm_json returns parsed JSON or None
        draft = call_llm_json([{"role": "system", "content": utils.WORKLOG_SYSTEM}, {"role": "user", "content": prompt}], model="extractor")
        if not draft: continue
        entries = draft.get("entries", [])
        combined_text = " ".join([t.get("user_turn","") + " " + t.get("text","") for t in batch])
        for e in entries:
            ok, reason = utils.verify_evidence(e.get("evidence",""), combined_text)
            e["_status"] = "auto" if ok else "flagged"
            e["_reason"] = reason
        total += len(entries)
    return total

def main():
    preflight_checks("worklog_generation_pipeline.py")
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    n = run(args.date, args.dry_run)
    print(f"worklog_generator_v2 done: {n} entries")

if __name__ == "__main__":
    main()
