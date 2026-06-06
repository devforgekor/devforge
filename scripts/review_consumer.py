#!/usr/bin/env python3
"""review_consumer.py — 27B production final verify.

Reads activity_log WHERE queue_status='reviewed' (after review_worker.py's
extract+14B debate / debate pipeline). Qwen3.6-27B makes final approve/reject/escalate
decisions and stores result as body.verify_result.

This is the PRODUCTION verify gate. Runs nightly via nightly_batch.sh Phase 5.
For experimental 32B parallel verify, see test_review_consumer.py.

Usage:
  python3 review_consumer.py [--limit N]       # 27B production final verify
"""

import http.client as hc
import json
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional

from lib.db import psql, psql_ok, esc_sql
from lib.feedback import get_feedback_for_model
from lib.llm.json_parser import parse_llm_json

REVIEW_HOST = "127.0.0.1"
REVIEW_PORT = 8081
REVIEW_TIMEOUT = 600
QUEUE_LIMIT = 50
MAX_REVERIFY = 2  # max times an item goes through re-verification

VERIFY_SYSTEM = """You are a final verification specialist for 27B production verify. Make the final decision on this item.

Review the original item and the 14B review result. Analyze step by step, then decide:
- approved: item is correct, can be committed/merged
- rejected: item has critical issues, must be reverted
- escalate: requires human or API-level review (DeepSeek Pro)

Break down your analysis into individual verification items. Each item checks one specific
aspect (evidence, consensus, security, etc.) and gets pass/fail.

Return STRICT JSON ONLY. No extra text, no markdown wrapper.
Schema:
{
  "reasoning": "step-by-step analysis of the item — what checks passed/failed, which parts are suspicious, what the 14B review got right or missed, and why the final decision was reached (3-5 sentences)",
  "verification_items": [
    {
      "check": "brief description of what is being verified (e.g. 'evidence matches the claim', '14B consensus aligns with evidence')",
      "result": "pass|fail",
      "detail": "one-sentence explanation of why this check passed or failed"
    }
  ],
  "final_verdict": "approved|rejected|escalate",
  "action": "commit|revert|escalate",
  "escalation_reason": "only if escalate — what specific question to ask the human or API reviewer",
  "confidence": 0-100,
  "summary": "1-sentence final decision"
}"""

ANALYSIS_SYSTEM = """You are a feedback rollback analyst. A new Gold Standard pattern set was injected
into the LLM feedback loop, but the verification pass rate dropped. Your job is to analyze WHY
the new patterns caused regression.

You are given:
- Previous generation's patterns (known-good, achieved higher pass rate)
- Failed generation's patterns (caused the drop)
- pass rates before and after

Diagnose each failed pattern:
1. Is it incorrect, misleading, or too narrow?
2. Does it contradict good behavior from the previous set?
3. Is it framed in a way that confuses rather than guides?
4. Propose a corrected version of each problematic pattern.

Return STRICT JSON:
{
  "root_cause": "brief diagnosis of why pass rate dropped (1-2 sentences)",
  "findings": [
    {
      "issue": "the problematic pattern's issue text",
      "problem": "why this pattern caused regression",
      "corrected_fix": "how the fix should be rewritten",
      "classification": "edge_case"
    }
  ],
  "verified_patterns": [
    {"issue": "a pattern from previous gen that remains valid", "fix": "its fix", "classification": "gold_standard"}
  ]
}

Only flag patterns where you can identify a concrete issue. Avoid speculation."""


def fetch_queue() -> List[Dict]:
    """Fetch items from activity_log queue WHERE queue_status='reviewed'."""
    sql = f"""SELECT id, type, source, title,
                     regexp_replace(summary, E'[\\n\\r\\\\|]+', ' ', 'g') AS summary,
                     regexp_replace(body::text, E'[\\n\\r\\\\|]+', ' ', 'g') AS body,
                     model, turn_ids, tags
              FROM activity_log
              WHERE queue_status = 'reviewed'
                AND type IN ('review', 'debate_result', 'analysis_request', 'extract_result')
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


def call_verify_model(messages: List[Dict], max_tokens: int = 768) -> Optional[Dict]:
    """Call Qwen3.6-27B on :8081. Returns parsed JSON or None.

    Falls back to reasoning_content if content is empty (reasoning bleed guard).
    """
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
    """Build prompt for Qwen-32B verification. Adapts to item type.

    Appends DeepSeek audit context for re-verification when present.
    """
    body = item["body"] or {}
    item_type = item.get("type", "")

    if item_type == "analysis_request":
        base = (
            f"## Rollback Analysis Request\n\n"
            f"Title: {item['title']}\n"
            f"Pass rate dropped: {body.get('failed_pass_rate', '?')} "
            f"(failed) vs {body.get('prev_pass_rate', '?')} (previous)\n\n"
        )
        failed = body.get("failed_patterns", [])
        if failed:
            base += "## Failed Generation Patterns (caused regression)\n"
            for p in failed:
                base += f"- [{p.get('classification','?')}] {p['issue']} → {p.get('fix','')[:200]}\n"
        prev_p = body.get("previous_patterns", [])
        if prev_p:
            base += "\n## Previous Generation Patterns (known-good)\n"
            for p in prev_p:
                base += f"- [{p.get('classification','?')}] {p['issue']} → {p.get('fix','')[:200]}\n"
    elif item_type == "debate_result":
        # From local_debate.py: multi-round debate output.
        # Body keys: session_id, question, method, mode, consensus_scores,
        #   consensus_trend, final_diff, confidence, action, rounds, etc.
        base = (
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
    elif item_type == "extract_result":
        # From extract_pipeline.py: 3B → Python verify → 30B MCP fields.
        # Body keys: turn_id, fact_count, extract_model, mark, mcp.
        mcp = body.get("mcp", {}) or {}
        verified = mcp.get("verified", {}) or {}
        verified_files = verified.get("files", [])
        verified_syms = verified.get("symbols", [])
        missing_files = [f["path"] for f in verified_files if not f.get("exists")]
        missing_syms = [s["name"] for s in verified_syms if not s.get("found")]
        base = (
            f"## Extract Pipeline Result\n\n"
            f"Turn: {body.get('turn_id', '?')}\n"
            f"Extract model: {body.get('extract_model', '?')}\n"
            f"Fact count: {body.get('fact_count', '?')}\n"
            f"Mark: {body.get('mark', '(none)')}\n\n"
            f"### MCP Fields\n"
            f"TLDR: {mcp.get('tldr', '')}\n"
            f"Intent: {mcp.get('intent', '?')}\n"
            f"Entities files: {mcp.get('entities', {}).get('files', [])}\n"
            f"Entities functions: {mcp.get('entities', {}).get('functions', [])}\n"
            f"Entities technologies: {mcp.get('entities', {}).get('technologies', [])}\n"
            f"Tags: {mcp.get('tags', [])}\n"
        )
        if missing_files:
            base += f"Missing files (not found on disk): {missing_files}\n"
        if missing_syms:
            base += f"Missing symbols (not found in codebase): {missing_syms}\n"
        base += (
            f"\nVerify that the MCP metadata is accurate. Check:\n"
            f"1. Is the TLDR factual and concise?\n"
            f"2. Are entity files/symbols/technologies correctly identified?\n"
            f"3. Are entity existence checks accurate?\n"
            f"4. Are tags useful for discovery and routing?"
        )
    else:
        # Activity review: DeepSeek audit or queue_writer enqueue.
        base = (
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

    # Append DeepSeek audit context if present (re-verification)
    deepseek_audit = body.get("deepseek_audit", {})
    if deepseek_audit:
        feedback_items = deepseek_audit.get("feedback_items", [])
        items_str = "\n".join(
            f"  - [{fi.get('audit_result', '?')}] {fi.get('check', '?')} — {fi.get('audit_reason', '')[:200]}"
            for fi in feedback_items
        )
        base += (
            f"\n\n## DeepSeek Audit Feedback (previous verification had issues)\n"
            f"Assessment: {deepseek_audit.get('overall_assessment', '?')}\n"
            f"Summary: {deepseek_audit.get('summary', '')}\n"
            f"Feedback items:\n{items_str}\n"
            f"\nAddress these concerns in your re-verification. If already fixed, confirm with 'pass'."
        )

    return base


def update_queue_status(item_id: str, result: Dict, is_analysis: bool = False) -> bool:
    """Update activity_log with verify/analysis result. queue_status → 'done'.

    For analysis items: stores result in body.analysis_result (not verify_result).
    For normal items: preserves deepseek fields, resets deepseek_reviewed.
    """
    body_merged = {}
    existing = psql(f"SELECT body FROM activity_log WHERE id = {esc_sql(str(item_id))}")
    if existing:
        try:
            body_merged = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            body_merged = {}

    if is_analysis:
        body_merged["analysis_result"] = result
    else:
        preserve = {}
        for key in ("deepseek_audit", "reverify_count", "deepseek_escalated"):
            if key in body_merged:
                preserve[key] = body_merged[key]
        body_merged["verify_result"] = result
        body_merged["deepseek_reviewed"] = False
    body_json = json.dumps(body_merged, ensure_ascii=False).replace("'", "''")
    return psql_ok(f"""UPDATE activity_log SET
        body = '{body_json}'::jsonb,
        queue_status = 'done',
        exec_status = 'DONE'
        WHERE id = {item_id}""")


def run_verify() -> int:
    """Process all reviewed items with 27B production verify.

    Skips items that have reached MAX_REVERIFY and marks them as escalated.
    """
    items = fetch_queue()
    if not items:
        print("No items in queue (queue_status='reviewed')")
        return 0

    print(f"[{datetime.now(timezone.utc).isoformat()}] 27B verify: {len(items)} items")

    ok_count = 0
    for i, item in enumerate(items):
        is_analysis = item.get("type") == "analysis_request"

        if not is_analysis:
            reverify_count = item["body"].get("reverify_count", 0)
            if reverify_count >= MAX_REVERIFY:
                print(f"    {item['id']} — max re-verify reached ({reverify_count}), escalating")
                body = item["body"]
                body["deepseek_escalated"] = True
                body_json = json.dumps(body, ensure_ascii=False).replace("'", "''")
                if psql_ok(f"""UPDATE activity_log SET
                    body = '{body_json}'::jsonb,
                    queue_status = 'done',
                    exec_status = 'DONE'
                    WHERE id = {item['id']}"""):
                    ok_count += 1
                continue

        user_prompt = build_verify_prompt(item)
        if is_analysis:
            messages = [{"role": "system", "content": ANALYSIS_SYSTEM}]
        else:
            messages = [{"role": "system", "content": VERIFY_SYSTEM}]
            feedback = get_feedback_for_model("Qwen27B", max_gold=1, max_edge=1)
            if feedback:
                messages.extend(feedback)
                print(f"    feedback injected: {len(feedback)//2} example(s)")
        messages.append({"role": "user", "content": user_prompt})

        print(f"  [{i+1}/{len(items)}] id={item['id']} type={item['type']} — {item['title'][:60]}")

        if is_analysis:
            result = call_verify_model(messages, max_tokens=1024)
        else:
            result = call_verify_model(messages)
        if not result:
            print(f"    FAILED — will retry next cycle")
            continue

        if update_queue_status(item["id"], result, is_analysis=is_analysis):
            ok_count += 1
            if is_analysis:
                n_findings = len(result.get("findings", []))
                print(f"    analysis done: {n_findings} finding(s), "
                      f"{result.get('root_cause', '')[:80]}")
            else:
                print(f"    {result.get('final_verdict', '?')} → done")
        else:
            print(f"    DB update failed")

    print(f"\n27B verify done: {ok_count}/{len(items)} processed")
    return ok_count


def main():
    import argparse
    ap = argparse.ArgumentParser(description="27B production final verify")
    ap.add_argument("--limit", type=int, default=50,
                    help="Max items per run (default: 50)")
    args = ap.parse_args()

    global QUEUE_LIMIT
    QUEUE_LIMIT = args.limit

    return 0 if run_verify() >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
