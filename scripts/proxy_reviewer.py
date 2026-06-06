#!/usr/bin/env python3
"""proxy_reviewer.py — DeepSeek Pro verification of 27B verify reasoning.

Reads activity_log items WHERE queue_status='done' AND verify_result exists but
has not been reviewed by DeepSeek Pro. Sends reasoning + verification_items to
api.deepseek.com for independent audit, stores feedback, and marks reviewed.

Designed as a batch process (nightly_batch Phase 7), not real-time loop.

Usage:
  python3 proxy_reviewer.py [--limit N]       # verify up to N items
  python3 proxy_reviewer.py --dry-run          # scan only, no API calls
"""

import csv
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.db import psql, psql_ok, esc_sql
from lib.llm.json_parser import parse_llm_json
from lib.llm.client import call_llm

DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BATCH_LIMIT = 50   # fetch up to 50, then group into token-based batches
BATCH_TARGET_MIN = 58000
BATCH_TARGET_MAX = 63000
BATCH_HARD_CAP = 64000
MAX_REVERIFY = 2  # max times an item gets re-queued for re-verification

# Each items gives ~300-800 prompt tokens; 10-30 fit per batch comfortably.
# Generation: ~200-400 tokens per item in a batch. For N items, scale max_tokens.
GEN_TOKENS_PER_ITEM = 512

DEEPSEEK_SYSTEM_SINGLE = """You are an independent verification auditor. Review the 27B model's
verification reasoning below. Check each verification_item for accuracy and soundness.

For each item, decide pass/fail. Provide a brief justification for any disagreement.

Return STRICT JSON:
{
  "overall_assessment": "agreed|disagreed|partial",
  "feedback_items": [
    {
      "check": "copy of the original verification_items[].check",
      "original_result": "pass|fail",
      "audit_result": "agree|disagree",
      "audit_reason": "why you agree or disagree (1-2 sentences)"
    }
  ],
  "summary": "overall assessment of the verification quality (1-2 sentences)",
  "flags": ["any concerns not captured in feedback_items"]
}"""

DEEPSEEK_SYSTEM_BATCH = """You are an independent verification auditor. You will receive MULTIPLE
items, each separated by a "---ITEM <id>---" marker. Review EACH item independently.

For each item, check the 27B model's reasoning and verification_items for accuracy and
soundness. Decide pass/fail per verification_item and provide justification.

Return a STRICT JSON object with ALL items' reviews as an array:

{
  "reviews": [
    {
      "item_id": "<exact id from ---ITEM <id>--- marker>",
      "overall_assessment": "agreed|disagreed|partial",
      "feedback_items": [
        {
          "check": "copy of the original verification check",
          "original_result": "pass|fail",
          "audit_result": "agree|disagree",
          "audit_reason": "why you agree or disagree (1-2 sentences)"
        }
      ],
      "summary": "overall assessment of this item (1-2 sentences)",
      "flags": ["any concerns not captured in feedback_items"]
    }
  ]
}

You MUST include exactly one review object per ---ITEM--- marker. Never skip items.
Output ONLY valid JSON. No extra text."""


def fetch_verify_queue(limit: int = BATCH_LIMIT) -> List[Dict]:
    """Fetch done items whose verify_result has NOT been DeepSeek-reviewed.

    Uses psql --csv output to safely handle JSON body containing pipes/commas.
    """
    sql = f"""SELECT id, type, source, title, summary, body::text, model
              FROM activity_log
              WHERE queue_status = 'done'
                AND exec_status = 'DONE'
                AND type IN ('review', 'debate_result', 'analysis_request')
                AND body->'verify_result' IS NOT NULL
                AND (body->>'deepseek_reviewed' IS NULL
                     OR body->>'deepseek_reviewed' = 'false')
              ORDER BY created_at ASC
              LIMIT {limit}"""
    r = subprocess.run(
        ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
         "-d", "devforge_app", "--csv", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return []

    items = []
    for row in csv.DictReader(io.StringIO(r.stdout)):
        if not row:
            continue
        try:
            body = json.loads(row["body"]) if row.get("body") else {}
        except (json.JSONDecodeError, KeyError):
            continue
        items.append({
            "id": row.get("id", ""),
            "type": row.get("type", ""),
            "source": row.get("source", ""),
            "title": row.get("title", ""),
            "summary": row.get("summary", ""),
            "body": body,
            "model": row.get("model", ""),
        })
    return items


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (chars/4). Good enough for batching thresholds."""
    return len(text) // 4


def build_audit_prompt(item: Dict) -> str:
    """Build prompt for DeepSeek Pro from verify_result."""
    verify = item["body"].get("verify_result", {})
    reasoning = verify.get("reasoning", "(no reasoning)")
    v_items = verify.get("verification_items", [])
    verdict = verify.get("final_verdict", "?")
    action = verify.get("action", "?")
    confidence = verify.get("confidence", "?")

    lines = [
        f"## Original Item",
        f"Type: {item['type']}",
        f"Title: {item['title']}",
        f"Summary: {item['summary'][:300]}" if item.get("summary") else "",
        "",
        f"## 27B Verification Result",
        f"Verdict: {verdict}",
        f"Action: {action}",
        f"Confidence: {confidence}",
        "",
        f"## 27B Reasoning",
        reasoning,
        "",
    ]

    if v_items:
        lines.append(f"## Verification Items ({len(v_items)} items)")
        for vi in v_items:
            check = vi.get("check", "?")
            result = vi.get("result", "?")
            detail = vi.get("detail", "")
            lines.append(f"- [{result.upper()}] {check}")
            if detail:
                lines.append(f"  Detail: {detail}")
        lines.append("")

    return "\n".join(lines)


def _build_batches(items: List[Dict]) -> List[List[Dict]]:
    """Group items into token-based batches (58K-63K per batch, hard cap 64K).

    Each batch stays under 64K prompt tokens so there is room for the generation
    response (~512 tokens per item). Items that individually exceed 64K are dropped.
    """
    system_tokens = _estimate_tokens(DEEPSEEK_SYSTEM_BATCH)
    batches = []
    current_batch = []
    current_tokens = system_tokens  # system prompt shared across the whole batch

    for item in items:
        item_prompt = f"---ITEM {item['id']}---\n{build_audit_prompt(item)}\n"
        item_tokens = _estimate_tokens(item_prompt)

        if item_tokens > BATCH_HARD_CAP:
            print(f"    SKIP id={item['id']}: {item_tokens}t exceeds hard cap")
            continue

        if current_tokens + item_tokens > BATCH_HARD_CAP:
            # Too large — flush current batch first
            if current_batch:
                batches.append(current_batch)
            current_batch = [item]
            current_tokens = system_tokens + item_tokens
        else:
            current_batch.append(item)
            current_tokens += item_tokens
            # If we hit target range, flush eagerly
            if BATCH_TARGET_MIN <= current_tokens <= BATCH_TARGET_MAX:
                batches.append(current_batch)
                current_batch = []
                current_tokens = system_tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def review_with_deepseek(item: Dict, dry_run: bool = False) -> Optional[Dict]:
    """Original single-item path — kept for single-item fallback."""
    return review_batch([item], dry_run=dry_run).get(item["id"])


def review_batch(batch_items: List[Dict], dry_run: bool = False) -> Dict[str, Dict]:
    """Send one batch of items to DeepSeek Pro in a single API call.

    Returns dict mapping item_id → feedback dict (same schema as review_with_deepseek).
    """
    if not DEEPSEEK_KEY and not dry_run:
        print(f"    SKIP — DEEPSEEK_API_KEY not set")
        return {}

    combined = "\n".join(
        f"---ITEM {item['id']}---\n{build_audit_prompt(item)}"
        for item in batch_items
    )

    if dry_run:
        print(f"    [dry-run] Would send to DeepSeek Pro ({len(batch_items)} items, ~{_estimate_tokens(combined)} prompt tokens)")
        return {item["id"]: {"overall_assessment": "dry_run", "feedback_items": [], "summary": "", "flags": []}
                for item in batch_items}

    gen_tokens = min(len(batch_items) * GEN_TOKENS_PER_ITEM, 4096)
    print(f"    Calling DeepSeek Pro ({len(batch_items)} items, ~{_estimate_tokens(combined)} prompt tokens, gen={gen_tokens})...")
    status, body = call_llm(
        "https://api.deepseek.com/v1/chat/completions",
        [
            {"role": "system", "content": DEEPSEEK_SYSTEM_BATCH if len(batch_items) > 1 else DEEPSEEK_SYSTEM_SINGLE},
            {"role": "user", "content": combined},
        ],
        api_key=DEEPSEEK_KEY,
        model="deepseek-chat",
        timeout=120,
        max_tokens=gen_tokens,
    )

    if status != 200:
        print(f"    DeepSeek API error: HTTP {status}")
        return {}

    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    result = parse_llm_json(content)
    if not result:
        print(f"    DeepSeek response parse failed (len={len(content)})")
        return {}

    reviews = result.get("reviews", [])
    if not reviews and len(batch_items) == 1:
        # Single-item response without array wrapper — wrap it
        reviews = [{**result, "item_id": batch_items[0]["id"]}]

    by_id = {}
    for r in reviews:
        rid = r.get("item_id", "")
        if rid:
            by_id[rid] = r
        else:
            print(f"    WARNING: review missing item_id")

    for item in batch_items:
        if item["id"] not in by_id:
            print(f"    WARNING: item {item['id']} missing from batch response")

    return by_id


def store_feedback(item_id: str, feedback: Dict) -> bool:
    """Store DeepSeek audit in body.deepseek_audit.

    Stores independently of body.verify_result so review_consumer's
    verify_result overwrite doesn't erase it.

    When disagreed/partial: re-queues to 'reviewed' for re-verification
    (up to MAX_REVERIFY times). After max: marks deepseek_escalated and
    leaves queue_status='done'.
    """
    existing = psql(f"SELECT body FROM activity_log WHERE id = {item_id}")
    if not existing:
        return False
    try:
        body_merged = json.loads(existing) if existing else {}
    except (json.JSONDecodeError, TypeError):
        body_merged = {}

    body_merged["deepseek_audit"] = feedback
    body_merged["deepseek_reviewed"] = True
    body_merged["deepseek_reviewed_at"] = datetime.now(timezone.utc).isoformat()

    assessment = feedback.get("overall_assessment", "agreed")
    reverify_count = body_merged.get("reverify_count", 0)
    queue_status = "done"

    if assessment in ("disagreed", "partial"):
        if reverify_count < MAX_REVERIFY:
            body_merged["reverify_count"] = reverify_count + 1
            queue_status = "reviewed"
        else:
            body_merged["deepseek_escalated"] = True

    body_json = json.dumps(body_merged, ensure_ascii=False).replace("'", "''")
    return psql_ok(f"""UPDATE activity_log SET
        body = '{body_json}'::jsonb,
        queue_status = '{queue_status}'
        WHERE id = {item_id}""")


def run_proxy_review(dry_run: bool = False, limit: int = BATCH_LIMIT) -> int:
    """Process all done items pending DeepSeek review — token-batched."""
    items = fetch_verify_queue(limit)
    if not items:
        print("No items pending DeepSeek review")
        return 0

    print(f"[{datetime.now(timezone.utc).isoformat()}] DeepSeek Pro review: {len(items)} items")

    batches = _build_batches(items)
    print(f"  Batched into {len(batches)} groups (target {BATCH_TARGET_MIN//1000}K-{BATCH_TARGET_MAX//1000}K tokens)")

    ok_count = 0
    for bi, batch in enumerate(batches):
        # Log batch summary
        tokens_est = sum(_estimate_tokens(build_audit_prompt(it)) for it in batch)
        print(f"  [batch {bi+1}/{len(batches)}] {len(batch)} items, ~{tokens_est} prompt tokens")
        for item in batch:
            verify = item["body"].get("verify_result", {})
            v_items = verify.get("verification_items", [])
            print(f"    {item['id']} type={item['type']} — {item['title'][:50]} ({len(v_items)} items)")

        feedback_map = review_batch(batch, dry_run=dry_run)
        if not feedback_map:
            print(f"    BATCH FAILED — will retry next cycle")
            continue

        for item in batch:
            feedback = feedback_map.get(item["id"])
            if not feedback:
                print(f"    {item['id']} — missing from response")
                continue

            if dry_run:
                print(f"    [dry-run] would store: {feedback.get('overall_assessment', '?')}")
                ok_count += 1
                continue

            if store_feedback(item["id"], feedback):
                assessment = feedback.get("overall_assessment", "?")
                n_flags = len(feedback.get("flags", []))
                re_status = ""
                if assessment in ("disagreed", "partial"):
                    rev = item["body"].get("reverify_count", 0)
                    if rev < MAX_REVERIFY:
                        re_status = " → re-queued for re-verify"
                    else:
                        re_status = " → max re-verify reached, escalated"
                print(f"    {item['id']} → {assessment}" + re_status + (f" ({n_flags} flags)" if n_flags else ""))
                ok_count += 1
            else:
                print(f"    {item['id']} — DB update failed")

    print(f"\nDeepSeek Pro review done: {ok_count}/{len(items)} processed ({len(batches)} batches)")
    return ok_count


def main():
    import argparse
    ap = argparse.ArgumentParser(description="DeepSeek Pro verification audit")
    ap.add_argument("--limit", type=int, default=BATCH_LIMIT, help="Max items per run")
    ap.add_argument("--dry-run", action="store_true", help="Scan only, no API calls")
    args = ap.parse_args()

    return 0 if run_proxy_review(dry_run=args.dry_run, limit=args.limit) >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
