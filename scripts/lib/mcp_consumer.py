"""mcp_consumer — read MCP metadata from review_facts and prepare for MCP tools.

Pre-build for Phase 2 (MCP tool integration). Reads fact_type='mcp_meta' facts,
formats them for MCP tools (mem_save, context injection), and marks as processed.

Flow:
  1. SELECT unprocessed mcp_meta facts
  2. Parse MCP fields (tldr, intent, entities, tags, verified)
  3. Format as MCP-ready structured data
  4. Update verdict to 'mcp_processed'

Usage:
  from lib.mcp_consumer import consume_mcp
  results = consume_mcp(limit=50)

CLI:
  python3 -m lib.mcp_consumer --limit 50 --json
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, esc_sql

BATCH_LIMIT = 50
MCP_FIELDS = ("tldr", "intent", "entities", "tags", "verified")


def fetch_unprocessed_mcp(limit: int = BATCH_LIMIT) -> List[Dict[str, Any]]:
    """Fetch review_facts rows where fact_type='mcp_meta' and verdict='pending'."""
    sql = (
        "SELECT rf.id, rf.turn_id, rf.evidence, rf.extract_model, "
        "  rf.created_at, t.seq, t.conversation_id "
        "FROM review_facts rf "
        "LEFT JOIN turns t ON t.id = rf.turn_id "
        "WHERE rf.fact_type = 'mcp_meta' "
        "  AND rf.verdict = 'pending' "
        "ORDER BY rf.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql(sql)
    if not rows:
        return []

    items = []
    for line in rows.split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        try:
            evidence = json.loads(parts[2])
        except (json.JSONDecodeError, IndexError):
            evidence = {}
        items.append({
            "id": parts[0].strip(),
            "turn_id": parts[1].strip(),
            "evidence": evidence,
            "extract_model": parts[3].strip(),
            "created_at": parts[4].strip(),
            "seq": int(parts[5]) if parts[5].strip().isdigit() else 0,
            "conversation_id": parts[6].strip() if len(parts) > 6 else "",
        })
    return items


def format_mcp_output(item: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a review_facts mcp_meta row to MCP-ready structured data."""
    ev = item.get("evidence", {})
    mcp_fields = {k: ev.get(k) for k in MCP_FIELDS if k in ev}

    return {
        "source": "extract_pipeline",
        "turn_id": item["turn_id"],
        "conversation_id": item["conversation_id"],
        "seq": item["seq"],
        "extract_model": item["extract_model"],
        "mcp": mcp_fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def mark_processed(fact_id: str) -> bool:
    """Mark a review_fact as processed by MCP consumer."""
    sql = (
        f"UPDATE review_facts SET verdict = 'mcp_processed' "
        f"WHERE id = {esc_sql(fact_id)}"
    )
    return psql_ok(sql)


def consume_mcp(limit: int = BATCH_LIMIT,
                dry_run: bool = False) -> List[Dict[str, Any]]:
    """Fetch unprocessed MCP facts, format for MCP tools, mark processed.

    Returns list of MCP-ready dicts.
    """
    items = fetch_unprocessed_mcp(limit)
    if not items:
        print("[mcp_consumer] No unprocessed MCP facts")
        return []

    print(f"[mcp_consumer] Processing {len(items)} MCP fact(s)")
    results = []
    for item in items:
        mcp_output = format_mcp_output(item)
        results.append(mcp_output)

        tldr = mcp_output["mcp"].get("tldr", "?")
        print(f"  [{item['turn_id'][:8]}] tldr={tldr}")

        if not dry_run:
            mark_processed(item["id"])

    print(f"[mcp_consumer] {len(results)} formatted"
          f" ({'dry-run' if dry_run else 'processed'})")
    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="MCP Consumer — read and format MCP metadata")
    parser.add_argument("--limit", "-n", type=int, default=BATCH_LIMIT)
    parser.add_argument("--dry-run", action="store_true",
                        help="Read only, no verdict update")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON lines")
    args = parser.parse_args()

    results = consume_mcp(limit=args.limit, dry_run=args.dry_run)

    if args.json:
        for r in results:
            print(json.dumps(r, ensure_ascii=False))

    sys.exit(0)


if __name__ == "__main__":
    main()
