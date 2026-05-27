#!/usr/bin/env python3
"""review_consumer.py — process activity_log queue through review/verify stages.

Stage 1 (14b): Qwen-14B reviews each queued item — adds findings, recommendation.
Stage 2 (32b): Qwen-32B final verification — approve/reject/escalate decision.

Each item processed independently — context reset between items.
Updates queue_status: unprocessed → reviewed_14b → verified_32b → done.

Usage:
  python3 review_consumer.py --stage 14b       # Qwen-14B review pass
  python3 review_consumer.py --stage 32b       # Qwen-32B verify pass
"""

import http.client as hc
import json
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, List

from lib.db import psql, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json

REVIEW_HOST = "127.0.0.1"
REVIEW_PORT = 8081
REVIEW_TIMEOUT = 600
QUEUE_LIMIT = 50

REVIEW_14B_SYSTEM = """You are a code review specialist. Review the item below and provide findings.

For debate_result items: verify the code diff is correct, identify missing edge cases, check security.
For review items: verify the extracted facts are accurate, flag any suspicious verdicts.

Return JSON:
{"review_verdict": "agree|disagree|partial", "found_issues": ["issue1", ...], "recommendation": "accept|revise|reject", "review_comment": "detailed review in 2-3 sentences"}"""

VERIFY_32B_SYSTEM = """You are a final verification specialist. Make the final decision on this item.

Review the original item and the Qwen-14B review. Decide:
- approved: item is correct, can be committed/merged
- rejected: item has critical issues, must be reverted
- escalate: requires human or API-level review (DeepSeek Pro)

Return JSON:
{"final_verdict": "approved|rejected|escalate", "action": "commit|revert|escalate", "escalation_reason": "why escalation needed (if escalate)", "confidence": 0.0-1.0, "summary": "1-sentence final decision"}"""


def fetch_queue(stage: str) -> List[Dict]:
    """Fetch items from activity_log queue for the given stage."""
    limit = QUEUE_LIMIT
    if stage == "14b":
        where = "queue_status = 'unprocessed'"
    elif stage == "32b":
        where = "queue_status = 'reviewed_14b'"
    else:
        raise ValueError(f"Unknown stage: {stage}")

    sql = f"""SELECT id, type, source, title, summary, body, model, turn_ids, tags
              FROM activity_log
              WHERE {where}
                AND type IN ('review', 'debate_result')
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


def call_review_model(messages: List[Dict], max_tokens: int = 512) -> Optional[Dict]:
    """Call review model on :8081. Returns parsed JSON or None."""
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


def build_review_prompt(item: Dict) -> str:
    """Build prompt for Qwen-14B review."""
    return (
        f"Type: {item['type']}\n"
        f"Title: {item['title']}\n"
        f"Summary: {item['summary']}\n"
        f"Details: {json.dumps(item['body'], ensure_ascii=False)[:2000]}"
    )


def build_verify_prompt(item: Dict) -> str:
    """Build prompt for Qwen-32B verification. Includes 14B review."""
    body = item["body"] or {}
    review_14b = body.get("review_14b", {})
    return (
        f"Type: {item['type']}\n"
        f"Title: {item['title']}\n"
        f"Summary: {item['summary']}\n"
        f"Original details: {json.dumps(body, ensure_ascii=False)[:1500]}\n\n"
        f"Qwen-14B Review: {json.dumps(review_14b, ensure_ascii=False)}"
    )


def update_queue_status(item_id: str, stage: str, result: Dict) -> bool:
    """Update activity_log with review result and advance queue_status."""
    body_merged = {}
    # Fetch existing body
    existing = psql(f"SELECT body FROM activity_log WHERE id = {item_id}")
    if existing:
        try:
            body_merged = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            body_merged = {}

    if stage == "14b":
        body_merged["review_14b"] = result
        new_status = "reviewed_14b"
    else:
        body_merged["review_32b"] = result
        new_status = "done"

    body_json = json.dumps(body_merged, ensure_ascii=False).replace("'", "''")
    return psql_ok(f"""UPDATE activity_log SET
        body = '{body_json}'::jsonb,
        queue_status = '{new_status}',
        exec_status = CASE WHEN '{new_status}' = 'done' THEN 'DONE' ELSE exec_status END
        WHERE id = {item_id}""")


def run_stage(stage: str) -> int:
    """Process all queued items for the given stage. Returns items processed."""
    items = fetch_queue(stage)
    if not items:
        print(f"No items in queue for stage {stage}")
        return 0

    system_prompt = REVIEW_14B_SYSTEM if stage == "14b" else VERIFY_32B_SYSTEM
    build_fn = build_review_prompt if stage == "14b" else build_verify_prompt
    stage_label = "Qwen-14B review" if stage == "14b" else "Qwen-32B verify"

    print(f"[{datetime.now(timezone.utc).isoformat()}] {stage_label}: {len(items)} items")

    ok_count = 0
    for i, item in enumerate(items):
        user_prompt = build_fn(item)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        print(f"  [{i+1}/{len(items)}] id={item['id']} type={item['type']} — {item['title'][:60]}")

        result = call_review_model(messages)
        if not result:
            print(f"    FAILED — will retry next cycle")
            continue

        if update_queue_status(item["id"], stage, result):
            ok_count += 1
            verdict_key = "review_verdict" if stage == "14b" else "final_verdict"
            print(f"    {result.get(verdict_key, '?')} → queue_status advanced")
        else:
            print(f"    DB update failed")

    print(f"\n{stage_label} done: {ok_count}/{len(items)} processed")
    return ok_count


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Review queue consumer")
    ap.add_argument("--stage", required=True, choices=["14b", "32b"],
                    help="Review stage: 14b (review) or 32b (verify)")
    ap.add_argument("--limit", type=int, default=50,
                    help="Max items per run (default: 50)")
    args = ap.parse_args()

    global QUEUE_LIMIT
    QUEUE_LIMIT = args.limit

    processed = run_stage(args.stage)
    return 0 if processed >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
