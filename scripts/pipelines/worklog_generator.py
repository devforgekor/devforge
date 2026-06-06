#!/usr/bin/env python3
# Status: production
# Path: 15m_cycle.sh
"""worklog_generator.py — 3-stage speculative pipeline: 3B draft → Python verify → 30B review.

1. Qwen2.5-Coder-3B (:8082) extracts worklog entries from turns (fast, bulk)
2. Python verify_evidence() — deterministic substring check, zero hallucination
3. Qwen3-Coder-30B-A3B (:8080) reviews only flagged entries (evidence mismatch)
   → auto if evidence semantically matches, flagged if hallucination confirmed

Speculative decoding pattern: cheap model drafts, expensive model verifies.
Triggered by 15-min systemd timer (daytime).
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.db import esc_sql, psql, psql_json, psql_ok
from lib.llm.json_parser import parse_llm_json
from lib.llm_client import call_llm, call_llm_json

KST = timezone(timedelta(hours=9))
BATCH_SIZE = 5    # turns per LLM call
MAX_BATCHES = 2   # max batches per agent per run
MAX_RETRIES = 1   # retry once on LLM failure before breaking batch

# Korean summary patterns — evidence that is summarization, not verbatim quotes
_KOREAN_SUMMARY_RE = re.compile(
    r"^(적용|분석|구현|수정|확인|테스트|배포|설정|제거|추가|변경|최적화|리팩토링|디버깅|문서화|통합|마이그레이션|롤백)"
    r"\s*(완료|종료|끝|마침|됨|했음|하였음|했습니다|하였습니다)",
)

WORKLOG_SYSTEM = """You are a worklog generator. From AI coding session turns, extract completed work items.

CRITICAL — Evidence rules:
1. Evidence = copy-paste a sentence verbatim from the turn text. Do NOT rewrite. Do NOT summarize.
2. Open the turn text, find the exact line, copy it with your cursor, paste it into the evidence field.
3. Evidence MUST be short: a command line, error message, log line, or decision sentence. Max 150 characters.
4. If you cannot find an exact line to copy, skip that entry entirely.
5. Never write tables, config blocks, multi-line text, or Korean summaries as evidence.

BAD evidence (hallucination — SKIP THESE):
  "적용 완료. 최종 구성: 설정 값 의미..."  ← this is a summary, not a quote
  "분석 완료. 현재 워크로그 시스템의..."   ← this is paraphrasing
  (empty)                                    ← fabricating without evidence

GOOD evidence (verbatim quotes from turn):
  "cat infrastructure.md"                    ← exact command from turn
  "Monitor timed out — re-arm if needed."    ← exact log line from turn
  "Stage 4 done: 345s, status=200"           ← exact output from turn

Other rules:
- One worklog entry = one completed task or notable outcome.
- Skip: status updates, "ok"/"yes" replies, conversation boilerplate.
- KEEP: decisions, code changes, bugs fixed, features added, refactors, reviews.
- Use Korean for title/summary. Evidence stays in original language.
- Set confidence per entry: 0-100 integer (how confident this is a real, valuable, self-contained work item).
- Set action per entry: keep=valuable work item, reject=trivial or not self-contained, escalate=possibly useful but needs verification.

Return STRICT JSON (no markdown, no explanation):
{"entries": [{"title": "short (ko)", "summary": "1-2 sentences (ko)",
              "evidence": "verbatim from turn", "agent": "claude-code|copilot|gemini|aider",
              "tags": ["tag1", "tag2"], "confidence": 0-100, "action": "keep|reject|escalate"}]}

If no completed work with real evidence found, return {"entries": []}."""


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def fetch_unlogged_turns(date_str: str = None) -> Dict[str, List[Dict]]:
    date_str = date_str or _today_kst()
    rows = psql_json(f"""
    SELECT t.id, t.agent, t.conversation_id,
           t.user_turn, t.text, t.created_at
    FROM turns t
    WHERE t.created_at::date = '{date_str}'::date
      AND t.id NOT IN (
          SELECT unnest(turn_ids)
          FROM worklog_entries
          WHERE turn_ids IS NOT NULL
            AND array_length(turn_ids, 1) > 0
      )
      AND length(t.user_turn) > 30
    ORDER BY t.agent, t.created_at
    """)

    if not rows:
        return {}

    grouped: Dict[str, List[Dict]] = {}
    for row in rows:
        agent = row.get("agent") or "unknown"
        grouped.setdefault(agent, []).append({
            "id": row["id"],
            "agent": agent,
            "conversation_id": row.get("conversation_id", ""),
            "user_turn": (row.get("user_turn") or "")[:500],
            "text": (row.get("text") or "")[:500],
            "created_at": row.get("created_at", ""),
        })
    return grouped


def build_worklog_prompt(turns: List[Dict], agent: str) -> (str, List[str]):
    """Build DeepSeek prompt + return ordered turn_ids for later review mapping."""
    snippets = []
    turn_ids = []
    for t in turns[:BATCH_SIZE]:
        turn_ids.append(t["id"])
        snippets.append(
            f"[{t['created_at']}] user: {t['user_turn'][:500]}\n"
            f"  assistant: {t['text'][:500]}"
        )
    return (
        f"Agent: {agent}\n"
        f"Turns:\n\n" + "\n\n".join(snippets)
    ), turn_ids


def check_endpoint(port: int) -> bool:
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except Exception:
        return False


REVIEW_SYSTEM = """You are a worklog reviewer. A Python script flagged entries because the evidence string was not found as an exact substring in the turn text. Your job: decide if this is a real hallucination or a false positive.

Check:
1. Does the evidence appear in the turn text with minor differences (whitespace, punctuation, line breaks)?
2. Is the entry's claim supported by the turn text, even if the exact string is different?
3. Verdict: "approved" if the entry is substantively correct (evidence matches semantically). "flagged" if it's truly fabricated.

Produce 1-2 verification_items capturing the key checks performed (evidence match, semantic correctness).
Set action: approve=all entries are sound, reject=clear hallucination, escalate=mixed or uncertain.

Return STRICT JSON:
{"reviews": [{"index": 0, "verdict": "approved|flagged", "reason": "short reason"}],
 "action": "approve|reject|escalate",
 "consensus_score": 0-100,
 "verification_items": [{"check": "description", "result": "pass|fail|partial", "detail": "explanation"}]}"""


def review_flagged(flagged_entries: List[Dict], turns: List[Dict]) -> tuple:
    """Qwen3-Coder-30B-A3B reviews only entries that failed Python evidence check.
    Returns (reviews: List[Dict], action: str, consensus_score: int)."""
    source_text = "\n".join(
        f"[{t['created_at']}] {t['user_turn'][:500]}\n{t['text'][:500]}"
        for t in turns[:BATCH_SIZE]
    )
    entries_json = json.dumps([
        {"index": i, "title": e.get("title", ""), "evidence": e.get("evidence", "")}
        for i, e in enumerate(flagged_entries)
    ], ensure_ascii=False, indent=2)
    messages = [
        {"role": "system", "content": REVIEW_SYSTEM},
        {"role": "user", "content": f"Source turn text:\n{source_text[:2000]}\n\nFlagged entries:\n{entries_json}"},
    ]
    raw = call_llm(messages, model="Qwen30B", max_tokens=512)
    result = parse_llm_json(raw) if raw else None
    if not result:
        return [], "escalate", 0
    return (
        result.get("reviews", []),
        result.get("action", "escalate"),
        int(result.get("consensus_score", 0) or 0),
    )


def verify_evidence(evidence: str, turn_text: str) -> tuple:
    """Deterministic check: evidence must be a verbatim substring of turn text.
    Returns (ok: bool, reason: str)."""
    if not evidence or not turn_text:
        return False, "empty evidence or turn text"
    if len(evidence) > 200:
        return False, f"evidence too long ({len(evidence)} chars) — likely paraphrasing"
    if _KOREAN_SUMMARY_RE.match(evidence):
        return False, "evidence looks like Korean summary, not verbatim quote"
    if "\n" in evidence and len(evidence) > 120:
        return False, "multi-line evidence — likely fabricated"
    if evidence in turn_text:
        return True, "evidence verified (substring match)"
    return False, "evidence NOT found in turn text"


def insert_worklog_entries(entries: List[Dict],
                           turn_ids: List[str], agent: str, date_str: str) -> int:
    """Insert worklog entries. Status from _status key (set by caller after verification)."""
    turn_array = "{" + ",".join(turn_ids) + "}" if turn_ids else "'{}'"
    inserted = 0

    for i, entry in enumerate(entries):
        title = esc_sql(entry.get("title", "")[:200])
        summary = esc_sql(entry.get("summary", "")[:500])
        agent_esc = esc_sql(agent)
        evidence = entry.get("evidence", "")
        tags = entry.get("tags", [])
        status = entry.get("_status", "auto")
        reason = entry.get("_reason", "")
        fact_action = entry.get("action", "keep")
        fact_confidence = int(entry.get("confidence", 0) or 0)

        details = json.dumps([{"evidence": evidence[:500],
                               "verify_reason": reason,
                               "action": fact_action,
                               "confidence": fact_confidence}], ensure_ascii=False)
        details_esc = details.replace("'", "''")
        tags_sql = "ARRAY[" + ",".join(f"'{esc_sql(t)}'" for t in tags) + "]" if tags else "'{}'"

        ok = psql_ok(
            f"INSERT INTO worklog_entries "
            f"(date, title, summary, details, turn_ids, tags, agent, status, kind) "
            f"VALUES ('{date_str}', '{title}', '{summary}', "
            f"'{details_esc}'::jsonb, '{turn_array}'::uuid[], {tags_sql}, "
            f"'{agent_esc}', '{status}', 'task') "
            f"ON CONFLICT (date, title) DO UPDATE SET status = EXCLUDED.status"
        )
        if ok:
            inserted += 1

    return inserted


METRICS_FILE = "/opt/ai_data/worklog_metrics.jsonl"


def _log_metrics(agent: str, batch_num: int, entries: List[Dict],
                 turn_count: int, date_str: str) -> None:
    """Append batch metrics to JSONL for dataset accumulation."""
    auto_count = sum(1 for e in entries if e.get("_status") == "auto")
    flagged_count = len(entries) - auto_count
    evidence_ok = sum(1 for e in entries if "verified" in e.get("_reason", ""))
    confidences = [int(e.get("confidence", 0) or 0) for e in entries if e.get("confidence")]
    actions = [e.get("action", "keep") for e in entries]
    metric = {
        "ts": datetime.now(KST).isoformat(),
        "date": date_str,
        "agent": agent,
        "batch": batch_num,
        "draft_model": "qwen2.5-coder-3b",
        "review_model": "qwen3-30b-a3b",
        "turns_in": turn_count,
        "entries_out": len(entries),
        "auto": auto_count,
        "flagged": flagged_count,
        "evidence_substring_ok": evidence_ok,
        "avg_confidence": round(sum(confidences) / len(confidences), 1) if confidences else 0,
        "action_counts": {a: actions.count(a) for a in set(actions)},
    }
    try:
        with open(METRICS_FILE, "a") as f:
            f.write(json.dumps(metric, ensure_ascii=False) + "\n")
    except Exception:
        pass  # metrics are best-effort


def run(date_str: str = None) -> int:
    kst_hour = datetime.now(KST).hour
    if kst_hour < 7:
        print("  worklog_generator: quiet window (00:00-07:00 KST) — skipping")
        return 0

    if not check_endpoint(8082):
        print("  worklog_generator: 3B :8082 not available — skipping")
        return 0

    date_str = date_str or _today_kst()
    grouped = fetch_unlogged_turns(date_str)

    if not grouped:
        print(f"  worklog_generator [{date_str}]: no unlogged turns")
        return 0

    total_entries = 0
    total_turns = sum(len(v) for v in grouped.values())
    print(f"  worklog_generator [{date_str}]: {total_turns} unlogged turns "
          f"across {len(grouped)} agents")

    for agent, turns in grouped.items():
        if len(turns) < 3:
            continue

        batch_num = 0
        offset = 0
        while offset < len(turns) and batch_num < MAX_BATCHES:
            batch_turns = turns[offset:offset + BATCH_SIZE]
            offset += len(batch_turns)
            batch_num += 1

            prompt, batch_turn_ids = build_worklog_prompt(batch_turns, agent)
            messages = [
                {"role": "system", "content": WORKLOG_SYSTEM},
                {"role": "user", "content": prompt},
            ]

            print(f"  {agent}[{batch_num}]: 3B drafting from {len(batch_turns)} turns...")
            draft = None
            for attempt in range(MAX_RETRIES + 1):
                raw = call_llm_json(messages, model="Qwen3B", max_tokens=256)
                draft = parse_llm_json(raw) if raw else None
                if draft:
                    break
                if attempt < MAX_RETRIES:
                    print(f"    3B failed (attempt {attempt+1}) — retrying...")
            if not draft:
                print(f"    3B failed after {MAX_RETRIES+1} attempts — will retry next cycle")
                break

            entries = draft.get("entries", [])
            if not entries:
                continue

            # Pre-filter: reject entries with empty/missing titles
            entries = [e for e in entries if e.get("title", "").strip() and len(e.get("title", "").strip()) > 1]

            # Pre-filter: deduplicate identical evidence within batch
            seen_evidence = set()
            deduped = []
            for e in entries:
                ev = e.get("evidence", "")
                if ev and ev not in seen_evidence:
                    seen_evidence.add(ev)
                    deduped.append(e)
            if len(deduped) < len(entries):
                print(f"    Dedup: {len(entries)} → {len(deduped)} entries (duplicate evidence)")
            entries = deduped

            if not entries:
                continue

            # Stage 2: Python deterministic evidence verification
            combined_text = " ".join(
                (t.get("user_turn", "") + " " + t.get("text", ""))
                for t in batch_turns
            )
            flagged_indices = []
            for i, e in enumerate(entries):
                evidence = e.get("evidence", "")
                ok, reason = verify_evidence(evidence, combined_text)
                if ok:
                    e["_status"] = "auto"
                    e["_reason"] = reason
                else:
                    e["_status"] = "flagged"
                    e["_reason"] = reason
                    flagged_indices.append(i)

            # Stage 3: Qwen3-Coder-30B-A3B reviews only flagged entries
            if flagged_indices and check_endpoint(8080):
                flagged_entries = [entries[i] for i in flagged_indices]
                print(f"    30B reviewing {len(flagged_entries)} flagged entries...")
                reviews, review_action, review_consensus = review_flagged(flagged_entries, batch_turns)
                review_map = {r.get("index", -1): r for r in reviews}
                for idx_in_flagged, global_idx in enumerate(flagged_indices):
                    review = review_map.get(idx_in_flagged, {})
                    if review.get("verdict") == "approved":
                        entries[global_idx]["_status"] = "auto"
                        entries[global_idx]["_reason"] = f"30B: {review.get('reason', 'semantic match')}"
                approved = sum(1 for r in reviews if r.get("verdict") == "approved")
                confirmed = len(flagged_indices) - approved
                print(f"    30B verdict: action={review_action} consensus={review_consensus} "
                      f"{approved} approved, {confirmed} flagged confirmed")

            n = insert_worklog_entries(entries, batch_turn_ids, agent, date_str)
            total_entries += n
            _log_metrics(agent, batch_num, entries, len(batch_turns), date_str)
            for e in entries:
                st = e.get("_status", "?")[:4]
                print(f"    [{st}] {e.get('title', '?')[:60]}")

    return total_entries


def main():
    import argparse
    ap = argparse.ArgumentParser(description="LLM auto-worklog: Qwen2.5-Coder-3B draft + Qwen3-Coder-30B-A3B review")
    ap.add_argument("--date", type=str, help="Date to process (YYYY-MM-DD, default: today KST)")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even if turns already logged")
    args = ap.parse_args()

    date_str = args.date or _today_kst()
    if args.date and not re.match(r"^\d{4}-\d{2}-\d{2}$", args.date):
        print(f"worklog_generator: invalid date format '{args.date}' — use YYYY-MM-DD")
        return 1
    if args.force:
        psql(f"UPDATE worklog_entries SET turn_ids = '{{}}'::uuid[] "
             f"WHERE date = '{date_str}'")

    n = run(date_str)
    print(f"worklog_generator done: {n} entries")
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
