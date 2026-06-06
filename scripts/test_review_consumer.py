#!/usr/bin/env python3
"""test_review_consumer.py — 32B experimental parallel verify.

Runs alongside 27B production verify (review_consumer.py) as an experimental
parallel gate. Reads items from activity_log, stores results as
body.test_verify_result without modifying queue_status.

27B production verify handles the authoritative queue_status='done' transition.

Usage:
  python3 test_review_consumer.py [--limit N]   # 32B experimental verify
"""

import http.client as hc
import json
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

from lib.db import psql, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json

REVIEW_HOST = "127.0.0.1"
REVIEW_PORT = 8081
REVIEW_TIMEOUT = 900
QUEUE_LIMIT = 20

VERIFY_SYSTEM = """You are an experimental test verification specialist (32B). Review the same items
as the production 27B verify and produce an independent assessment. Your result is stored
as test_verify_result for comparison — it does NOT change the item's queue status.

Return STRICT JSON ONLY. No extra text, no markdown wrapper.
Schema:
{
  "reasoning": "step-by-step analysis (3-5 sentences)",
  "verification_items": [
    {
      "check": "brief description of what is being verified",
      "result": "pass|fail",
      "detail": "one-sentence explanation"
    }
  ],
  "final_verdict": "approved|rejected|escalate",
  "action": "commit|revert|escalate",
  "escalation_reason": "only if escalate",
  "confidence": 0-100,
  "summary": "1-sentence final decision"
}"""


def fetch_queue(limit: int = QUEUE_LIMIT) -> List[Dict]:
    """Fetch items from activity_log where 27B verify has run but test_verify hasn't."""
    sql = f"""SELECT id, type, source, title,
                     regexp_replace(summary, E'[\\n\\r\\\\|]+', ' ', 'g') AS summary,
                     regexp_replace(body::text, E'[\\n\\r\\\\|]+', ' ', 'g') AS body,
                     model, turn_ids, tags
              FROM activity_log
              WHERE queue_status IN ('reviewed', 'done')
                AND type IN ('review', 'debate_result')
                AND (body->'test_verify_result' IS NULL
                     OR body->'test_verify_result' = 'null'::jsonb)
              ORDER BY created_at ASC
              LIMIT {limit}"""
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


def call_verify_model(messages: List[Dict], max_tokens: int = 1024) -> Optional[Dict]:
    """Call 32B on :8081. Returns parsed JSON or None."""
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
        msg = data.get("choices", [{}])[0].get("message", {})
        content = msg.get("content", "")
        if not content.strip():
            reasoning = msg.get("reasoning_content", "")
            if reasoning:
                content = reasoning
                print(f"    (fell back to reasoning_content, {len(reasoning)} chars)")
        return parse_llm_json(content)
    except Exception as e:
        print(f"  LLM call failed: {e}")
        return None


def build_verify_prompt(item: Dict) -> str:
    """Build prompt for 32B test verify. Same context as 27B verify."""
    body = item["body"] or {}
    item_type = item.get("type", "")

    if item_type == "debate_result":
        return (
            f"Type: {item_type}\n"
            f"Title: {item['title']}\n"
            f"Summary: {item['summary']}\n"
            f"Debate question: {body.get('question', '?')[:200]}\n"
            f"Debate mode: {body.get('mode', '?')}, rounds: {body.get('rounds', '?')}\n"
            f"Consensus scores: {body.get('consensus_scores', [])}\n"
            f"Consensus trend: {body.get('consensus_trend', '?')}\n"
            f"Final confidence: {body.get('confidence', '?')}, "
            f"Final action: {body.get('action', '?')}\n"
            f"Final diff (first 1000 chars): {body.get('final_diff', '')[:1000]}"
        )
    else:
        return (
            f"Type: {item_type}\n"
            f"Title: {item['title']}\n"
            f"Summary: {item['summary']}\n"
            f"Original details: {json.dumps(body, ensure_ascii=False)[:1500]}\n\n"
            f"14B Review verdict: {body.get('review_verdict', 'pending')}, "
            f"consensus: {body.get('review_consensus', '?')}\n"
            f"14B Review summary: {body.get('review_summary', '')}\n"
            f"Fact confidence: {body.get('fact_confidence', '?')}, "
            f"Fact action: {body.get('fact_action', '?')}, "
            f"Review action: {body.get('review_action', '?')}"
        )


def store_test_result(item_id: str, result: Dict) -> bool:
    """Store test_verify_result without changing queue_status."""
    existing = psql(f"SELECT body FROM activity_log WHERE id = {esc_sql(str(item_id))}")
    body_merged = {}
    if existing:
        try:
            body_merged = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            body_merged = {}

    body_merged["test_verify_result"] = result
    body_json = json.dumps(body_merged, ensure_ascii=False).replace("'", "''")
    return psql_ok(f"""UPDATE activity_log SET
        body = '{body_json}'::jsonb
        WHERE id = {item_id}""")


def run_test_verify() -> int:
    """Process all items with 32B test verify (parallel to 27B)."""
    items = fetch_queue()
    if not items:
        print("No items for 32B test verify")
        return 0

    print(f"[{datetime.now(timezone.utc).isoformat()}] 32B test verify: {len(items)} items")

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

        if store_test_result(item["id"], result):
            ok_count += 1
            print(f"    {result.get('final_verdict', '?')} → test_verify stored")
        else:
            print(f"    DB update failed")

    print(f"\n32B test verify done: {ok_count}/{len(items)} processed")
    return ok_count


def main():
    import argparse
    ap = argparse.ArgumentParser(description="32B experimental parallel verify")
    ap.add_argument("--limit", type=int, default=20,
                    help="Max items per run (default: 20)")
    args = ap.parse_args()

    global QUEUE_LIMIT
    QUEUE_LIMIT = args.limit

    return 0 if run_test_verify() >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
