#!/usr/bin/env python3
# Status: production
# Path: 15m_cycle.sh
"""Day pre-review: P(day_p)→R(day_r)→J(day_j) for night prepill defense.

Each cycle reads unclassified turns (created_at > classify checkpoint)
that already have extract facts in review_facts.

Flow:
  Phase P (day_p):  turn + facts → findings (JSON, <300 lines)
  Phase R (day_r):  findings → accept/reject verdicts (JSON, <300 lines)
  Phase J (day_j):  findings + verdicts → score + decision (JSON, <300 lines)
  Save:          activity_log type='day_review', queue_status='pre_reviewed'
  Advance:       checkpoint on success, marker on failure

Models: day_p + day_j on Pod B (:8080), day_r on Pod A (:8082).
Both already running in day mode — no container management needed.

Usage:
  python3 classify_pipeline.py                 # process from checkpoint
  python3 classify_pipeline.py --limit 5       # batch cap
  python3 classify_pipeline.py --dry-run       # simulate, no writes
"""

import json
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS_DIR)

from lib.db import psql, psql_ok, esc_sql, psql_json
from lib.llm_client import call_llm
from lib.llm.json_parser import save_dlq, parse_llm_json
from lib.queue_writer import enqueue_review

BATCH_LIMIT = 5
TEMP = 0.1
MAX_TOKENS = 2048
TIMEOUT = 180
DAY_REVIEW_MAX_LINES = 300

DRY_RUN = "--dry-run" in sys.argv


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── System prompts ───────────────────────────────────────────────────
# Hallucination control: extract evidence only, JSON forced, <300 lines.

SYS_P = """You are a code review assistant. Examine the turn and extracted facts below.
Generate findings about potential issues, bugs, or improvements.

CRITICAL RULES:
- Each finding MUST cite specific evidence from the extracted facts.
- If no clear issue exists, return an empty findings list.
- Do NOT fabricate code, file paths, or function names.
- Do NOT guess. If uncertain, leave it out.
- Maximum 20 findings per turn.

Return JSON:
{
  "findings": [
    {
      "id": "D001",
      "severity": "critical|high|medium|low",
      "category": "bug|security|data_loss|performance|quality",
      "description": "1 sentence, under 150 chars",
      "evidence": "quote from extracted facts that supports this finding"
    }
  ]
}"""

SYS_R = """You are a verdict reviewer. For each finding proposed by the reviewer,
decide ACCEPT or REJECT based ONLY on whether the evidence supports the finding.

CRITICAL RULES:
- ACCEPT: evidence clearly supports the finding.
- REJECT: evidence is weak, missing, or contradicts the finding.
- REJECT: duplicate of another finding.
- Do NOT add new findings or modify existing ones.
- Be concise. One sentence per verdict.
- Keep response under 300 lines.

Return JSON:
{
  "verdicts": [
    {"id": "D001", "verdict": "accept", "reason": "evidence supports this finding"},
    {"id": "D002", "verdict": "reject", "reason": "evidence does not support"}
  ]
}"""

SYS_J = """You are a scoring judge. Review the findings and verdicts.
Assign a simple score and make a decision.

CRITICAL RULES:
- P_score (0-30) = quality of findings (correctness + coverage + precision)
- R_score (0-30) = quality of verdicts (accuracy + efficiency)
- decision = APPROVED if majority accepted and no critical findings rejected
- decision = REJECT otherwise
- Keep response under 300 lines.
- Only reference finding IDs that actually exist in the data below.

Return JSON:
{
  "P_score": 0-30,
  "P_rubric": {"correctness": 0-10, "coverage": 0-10, "precision": 0-10},
  "R_score": 0-30,
  "R_rubric": {"accuracy": 0-10, "efficiency": 0-10},
  "decision": "APPROVED|REJECT",
  "consensus_score": 0-100,
  "approved": ["D001"],
  "rejected": ["D002"],
  "report": {
    "summary": "1 sentence",
    "top_issues": ["most critical finding in 1 line"]
  }
}"""


# ── DB helpers ─────────────────────────────────────────────────────────

def _get_checkpoint():
    """Return max_created_at from pipeline_checkpoint for classify phase."""
    return psql("SELECT max_created_at::text FROM pipeline_checkpoint WHERE phase = 'classify'") or '-infinity'


def _advance_checkpoint(created_at_str):
    """Advance checkpoint to created_at if newer."""
    psql_ok(
        f"UPDATE pipeline_checkpoint "
        f"SET max_created_at = '{esc_sql(created_at_str)}'::timestamptz, "
        f"    updated_at = NOW() "
        f"WHERE phase = 'classify' "
        f"  AND max_created_at < '{esc_sql(created_at_str)}'::timestamptz"
    )


def _get_turn_facts(turn_id):
    """Query extracted facts for a turn (non-marker)."""
    sql = (
        "SELECT fact_index, fact_type, evidence, extract_model, verdict "
        f"FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND verdict != 'system' "
        "ORDER BY fact_index ASC"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    facts = []
    for row in rows:
        facts.append({
            "fact_index": row.get("fact_index", 0) or 0,
            "fact_type": row.get("fact_type", ""),
            "evidence": row.get("evidence", ""),
            "extract_model": row.get("extract_model", ""),
            "verdict": row.get("verdict", ""),
        })
    return facts


def _has_day_review(turn_id):
    """Check if a day_review already exists for this turn."""
    sql = (
        f"SELECT 1 FROM activity_log "
        f"WHERE type = 'day_review' "
        f"  AND body->>'turn_id' = '{esc_sql(turn_id)}' "
        "LIMIT 1"
    )
    return bool(psql(sql))


def _already_has_facts(turn_id):
    """Check if turn has been extracted (review_facts exist)."""
    sql = (
        f"SELECT 1 FROM review_facts "
        f"WHERE turn_id = '{esc_sql(turn_id)}'::uuid "
        f"  AND verdict != 'system' "
        "LIMIT 1"
    )
    return bool(psql(sql))


# ── Turn selection ────────────────────────────────────────────────────

def _get_unclassified_turns(limit=BATCH_LIMIT):
    """Return turns that have extract facts but no day_review yet."""
    checkpoint = _get_checkpoint()
    sql = (
        "SELECT t.id, t.user_turn, t.thinking, t.text, "
        "  t.source_message_id, t.created_at, "
        "  t.conversation_id, t.seq "
        "FROM turns t "
        "WHERE t.text != '' "
        f"  AND t.created_at > '{esc_sql(checkpoint)}'::timestamptz "
        "ORDER BY t.created_at ASC "
        f"LIMIT {limit}"
    )
    rows = psql_json(sql)
    if not rows:
        return []
    turns = []
    for row in rows:
        turns.append({
            "id": row.get("id", ""),
            "user_turn": row.get("user_turn", ""),
            "thinking": row.get("thinking") or None,
            "text": row.get("text", ""),
            "source_message_id": row.get("source_message_id", ""),
            "created_at": row.get("created_at", ""),
            "conversation_id": row.get("conversation_id", ""),
            "seq": row.get("seq", 0) or 0,
        })
    return turns


# ── LLM call helpers ──────────────────────────────────────────────────

def _call_json(messages, model, max_tokens=MAX_TOKENS, label=""):
    """Call LLM and parse JSON result. Uses shared parse_llm_json + DLQ."""
    meta = call_llm(messages, model=model, max_tokens=max_tokens,
                    timeout=TIMEOUT, json_mode=True, return_meta=True)
    raw = meta["content"]
    if isinstance(raw, str):
        raw = raw.strip()
    result = parse_llm_json(raw)
    if result is None:
        save_dlq(raw, stage=f"classify_{label}", model=model,
                 error="parse_llm_json returned None", attempt=1)
        log(f"  JSON parse error {label}: None after parse_llm_json")
    return result


# ── Phases ────────────────────────────────────────────────────────────

def _phase_p(turn, facts):
    """P(day_p): Generate findings from turn + facts."""
    ut = turn.get("user_turn", "") or ""
    th = turn.get("thinking", "") or ""
    tx = turn.get("text", "") or ""

    fact_lines = [f"[{f['fact_type']}] {f['evidence'][:200]}" for f in facts]
    facts_text = "\n".join(fact_lines) if fact_lines else "(no facts)"

    ctx = (
        "=== USER TURN ===\n"
        f"{ut[:2000]}\n\n"
        "=== THINKING ===\n"
        f"{th[:2000]}\n\n"
        "=== RESPONSE ===\n"
        f"{tx[:2000]}\n\n"
        "=== EXTRACTED FACTS ===\n"
        f"{facts_text}"
    )

    if DRY_RUN:
        log(f"  [DRY] P findings: mock")
        return [{"id": "D001", "severity": "medium", "category": "quality",
                  "description": "Dry-run finding", "evidence": "mock evidence"}]

    result = _call_json(
        [{"role": "system", "content": SYS_P},
         {"role": "user", "content": ctx}],
        model="day_p", label="P_classify"
    )
    findings = result.get("findings", []) if result else []
    log(f"  P(day_p): {len(findings)} findings")
    return findings


def _phase_r(findings):
    """R(day_r): Reflect on P's findings."""
    if not findings:
        log(f"  R(day_r): no findings to review")
        return []

    ctx = f"Review these findings:\n{json.dumps(findings, ensure_ascii=False, indent=2)[:4000]}"

    if DRY_RUN:
        log(f"  [DRY] R verdicts: mock")
        return [{"id": f.get("id"), "verdict": "accept",
                  "reason": "dry-run accept"} for f in findings[:3]]

    result = _call_json(
        [{"role": "system", "content": SYS_R},
         {"role": "user", "content": ctx}],
        model="day_r", label="R_classify"
    )
    verdicts = result.get("verdicts", []) if result else []
    log(f"  R(day_r): {len(verdicts)} verdicts")
    return verdicts


def _phase_j(findings, verdicts):
    """J(day_j): Score and decide."""
    ctx_parts = [
        f"Findings ({len(findings)}):\n",
        json.dumps(findings, ensure_ascii=False, indent=2)[:2000],
        f"\nVerdicts ({len(verdicts)}):\n",
        json.dumps(verdicts, ensure_ascii=False, indent=2)[:2000],
    ]

    if DRY_RUN:
        log(f"  [DRY] J result: mock")
        approved = [f.get("id") for f in findings[:2]]
        rejected = [f.get("id") for f in findings[2:]]
        return {
            "P_score": 20, "R_score": 18, "decision": "APPROVED",
            "consensus_score": 80, "approved": approved, "rejected": rejected,
            "report": {"summary": "Dry-run J decision", "top_issues": []}
        }

    result = _call_json(
        [{"role": "system", "content": SYS_J},
         {"role": "user", "content": "\n".join(ctx_parts)}],
        model="day_j", label="J_classify"
    )
    if result and result.get("decision") in ("APPROVED", "REJECT"):
        log(f"  J(day_j): P_score={result.get('P_score','?')} "
            f"R_score={result.get('R_score','?')} "
            f"decision={result.get('decision','?')}")
    else:
        log(f"  J(day_j): no valid result")
        result = None
    return result


# ── Main pipeline ─────────────────────────────────────────────────────

def classify_pipeline(limit=BATCH_LIMIT):
    """Day pre-review: P(day_p)→R(day_r)→J(day_j) for each unclassified turn."""
    t_start = time.monotonic()

    print(f"\n{'=' * 60}")
    print("Day Pre-Review Pipeline — P(day_p) → R(day_r) → J(day_j)")
    if DRY_RUN:
        print("  [DRY RUN] No writes to DB")
    print(f"{'=' * 60}")

    turns = _get_unclassified_turns(limit)
    if not turns:
        log("No unclassified turns found")
        return {"processed": 0, "failed": 0, "ok": True}

    # Filter: only turns with extract facts and no existing day_review
    eligible = []
    for t in turns:
        tid = t["id"]
        if not _already_has_facts(tid):
            continue
        if _has_day_review(tid):
            continue
        eligible.append(t)

    if not eligible:
        log("No eligible turns (all missing extract or already classified)")
        # Advance checkpoint past these turns anyway
        last_ts = turns[-1]["created_at"]
        _advance_checkpoint(last_ts)
        return {"processed": 0, "failed": 0, "ok": True}

    log(f"Eligible turns: {len(eligible)}")
    processed = 0
    failed = 0
    failed_marks = []

    for idx, turn in enumerate(eligible, 1):
        tid = turn["id"]
        ut = (turn.get("user_turn") or "")[:40]
        log(f"\n[{idx}/{len(eligible)}] Turn {tid[:8]}... \"{ut}\"")

        facts = _get_turn_facts(tid)
        log(f"  facts: {len(facts)}")

        # Phase P: day_p findings
        findings = _phase_p(turn, facts)

        # Python dedup before R (evidence-based, zero hallucination)
        seen_ev = set()
        deduped = []
        for f in findings:
            ev = (f.get("evidence") or "")[:100]
            if ev and ev not in seen_ev:
                seen_ev.add(ev)
                deduped.append(f)
        if len(deduped) < len(findings):
            log(f"  dedup: {len(findings)} → {len(deduped)} (removed {len(findings)-len(deduped)})")
        findings = deduped

        # Phase R: day_r verdicts
        verdicts = _phase_r(findings)

        # Phase J: day_j score
        j_result = _phase_j(findings, verdicts)

        if not j_result:
            log(f"  SKIP — J failed or empty, will retry next cycle")
            failed += 1
            continue

        # Save to activity_log
        body = {
            "turn_id": tid,
            "findings_count": len(findings),
            "verdicts_count": len(verdicts),
            "P_score": j_result.get("P_score", 0),
            "R_score": j_result.get("R_score", 0),
            "consensus_score": j_result.get("consensus_score", 0),
            "decision": j_result.get("decision", ""),
            "P_results": findings,
            "R_results": verdicts,
            "J_results": j_result,
        }

        if DRY_RUN:
            log(f"  [DRY] Would save day_review ({len(findings)} findings, "
                f"{j_result.get('decision','?')})")
            log(f"  [DRY] Would advance checkpoint to {turn['created_at']}")
            processed += 1
            continue

        enqueue_review(
            entry_type="day_review",
            source="classify_pipeline.py",
            title=f"Day review: {tid[:8]} ({len(findings)} findings)",
            summary=f"P-R-J day review: P_score={j_result.get('P_score','?')} "
                    f"decision={j_result.get('decision','?')}",
            body=body,
            queue_status="pre_reviewed",
        )

        _advance_checkpoint(turn["created_at"])
        log(f"  Saved day_review, checkpoint advanced")
        processed += 1

    elapsed = time.monotonic() - t_start
    log(f"\nPipeline complete: {processed} processed, {failed} failed ({elapsed:.1f}s)")

    if failed_marks:
        for m in failed_marks:
            log(f"  Failed: {m}")

    return {"processed": processed, "failed": failed, "ok": failed == 0}


def main():
    limit = BATCH_LIMIT
    for i, a in enumerate(sys.argv):
        if a == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    result = classify_pipeline(limit=limit)
    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
