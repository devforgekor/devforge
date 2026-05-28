#!/usr/bin/env python3
"""review_consumer.py — 32B final verification pass.

Reads activity_log WHERE queue_status='reviewed' (after review_worker.py's
extract+14B debate). Qwen-32B makes final approve/reject/escalate decision.

1차 검토는 review_worker.py가 담당 (extract + 14B debate).
이 스크립트는 32B 최종 판정만 수행.

Usage:
  python3 review_consumer.py [--limit N]       # Qwen-32B final verification
"""

import http.client as hc
import json
import sys
from datetime import datetime, timezone
from typing import Dict, List

from lib.db import psql, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json

REVIEW_HOST = "127.0.0.1"
REVIEW_PORT = 8081
REVIEW_TIMEOUT = 600
QUEUE_LIMIT = 50

VERIFY_SYSTEM = """You are a final verification specialist. Make the final decision on this item.

Review the original item and the 14B review result. Decide:
- approved: item is correct, can be committed/merged
- rejected: item has critical issues, must be reverted
- escalate: requires human or API-level review (DeepSeek Pro)

Return JSON:
{"final_verdict": "approved|rejected|escalate", "action": "commit|revert|escalate", "escalation_reason": "why escalation needed (if escalate)", "confidence": 0.0-1.0, "summary": "1-sentence final decision"}"""


def fetch_queue() -> List[Dict]:
    """Fetch items from activity_log queue WHERE queue_status='reviewed'."""
    sql = f"""SELECT id, type, source, title,
                     regexp_replace(summary, E'[\\n\\r\\\\|]+', ' ', 'g') AS summary,
                     regexp_replace(body::text, E'[\\n\\r\\\\|]+', ' ', 'g') AS body,
                     model, turn_ids, tags
              FROM activity_log
              WHERE queue_status = 'reviewed'
                AND type IN ('review', 'debate_result')
              ORDER BY created_at ASC
              LIMIT {QUEUE_LIMIT}"""
    rows = psql(sql, timeout=30)
    if not rows:
        return []

    items = []
    for line in rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 8)
        if len(parts) < 7:
            continue
        try:
            body = json.loads(parts[5]) if parts[5] else {}
        except json.JSONDecodeError:
            body = {"raw": str(parts[5])[:500]}
        items.append({
            "id": parts[0],
            "type": parts[1],
            "source": parts[2],
            "title": parts[3],
            "summary": parts[4],
            "body": body,
            "model": parts[6],
            "turn_ids": parts[7] if len(parts) > 7 else "",
            "tags": parts[8] if len(parts) > 8 else "",
        })
    return items


def call_verify_model(messages: List[Dict], max_tokens: int = 512) -> Optional[Dict]:
    """Call Qwen-32B on :8081. Returns parsed JSON or None."""
    body = json.dumps({
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
    })
    try:
        conn = hc.HTTPConnection(REVIEW_HOST, REVIEW_PORT, timeout=REVIEW_TIMEOUT)
        conn.request("POST", "/v1/chat/completions", body,
                     {"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        conn.close()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return parse_llm_json(content)
    except Exception as e:
        print(f"  LLM call failed: {e}")
        return None


def build_verify_prompt(item: Dict) -> str:
    """Build prompt for Qwen-32B verification. Adapts to item type."""
    body = item["body"] or {}
    item_type = item.get("type", "")

    if item_type == "debate_result":
        # From local_debate.py: multi-round debate output.
        # Body keys: session_id, question, method, mode, consensus_scores,
        #   consensus_trend, final_diff, confidence, rounds, etc.
        return (
            f"Type: {item_type}\n"
            f"Title: {item['title']}\n"
            f"Summary: {item['summary']}\n"
            f"Debate question: {body.get('question', '?')[:200]}\n"
            f"Debate mode: {body.get('mode', '?')}, rounds: {body.get('rounds', '?')}\n"
            f"Consensus scores: {body.get('consensus_scores', [])}\n"
            f"Consensus trend: {body.get('consensus_trend', '?')}\n"
            f"Final confidence: {body.get('confidence', '?')}\n"
            f"Final diff (first 1000 chars): {body.get('final_diff', '')[:1000]}"
        )
    else:
        # From review_worker.py: extract+verify+14B debate review.
        # Body keys: fact_index, fact_type, evidence, verdict, reason,
        #   extract_model, review_verdict, review_consensus, review_summary.
        return (
            f"Type: {item_type}\n"
            f"Title: {item['title']}\n"
            f"Summary: {item['summary']}\n"
            f"Original details: {json.dumps(body, ensure_ascii=False)[:1500]}\n\n"
            f"14B Review verdict: {body.get('review_verdict', 'pending')}, "
            f"consensus: {body.get('review_consensus', '?')}\n"
            f"14B Review summary: {body.get('review_summary', '')}"
        )


def update_queue_status(item_id: str, result: Dict) -> bool:
    """Update activity_log with verify result. queue_status → 'done'."""
    body_merged = {}
    existing = psql(f"SELECT body FROM activity_log WHERE id = {esc_sql(str(item_id))}")
    if existing:
        try:
            body_merged = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            body_merged = {}

    body_merged["verify_result"] = result
    body_json = json.dumps(body_merged, ensure_ascii=False).replace("'", "''")
    return psql_ok(f"""UPDATE activity_log SET
        body = '{body_json}'::jsonb,
        queue_status = 'done',
        exec_status = 'DONE'
        WHERE id = {item_id}""")


def run_verify() -> int:
    """Process all reviewed items with 32B final verification."""
    items = fetch_queue()
    if not items:
        print("No items in queue (queue_status='reviewed')")
        return 0

    print(f"[{datetime.now(timezone.utc).isoformat()}] Qwen-32B verify: {len(items)} items")

    ok_count = 0
    for i, item in enumerate(items):
        user_prompt = build_verify_prompt(item)
        messages = [
            {"role": "system", "content": VERIFY_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]

        print(f"  [{i+1}/{len(items)}] id={item['id']} type={item['type']} — {item['title'][:60]}")

        result = call_verify_model(messages)
        if not result:
            print(f"    FAILED — will retry next cycle")
            continue

        if update_queue_status(item["id"], result):
            ok_count += 1
            print(f"    {result.get('final_verdict', '?')} → done")
        else:
            print(f"    DB update failed")

    print(f"\nQwen-32B verify done: {ok_count}/{len(items)} processed")
    return ok_count


def main():
    import argparse
    ap = argparse.ArgumentParser(description="32B final verification pass")
    ap.add_argument("--limit", type=int, default=50,
                    help="Max items per run (default: 50)")
    args = ap.parse_args()

    global QUEUE_LIMIT
    QUEUE_LIMIT = args.limit

    return 0 if run_verify() >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
