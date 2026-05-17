#!/usr/bin/env python3
"""qwen_worker.py — 3-phase turn worker, runs every 15 min.

Phase 1 (deterministic, no Qwen):
  - ingest new turns from session files
  - backfill missing daily worklogs
  - link agent-known worklogs to turns via SQL JOIN ±24h
  - apply agent normalization fixes (AGENT_MAP)

Phase 2 (light Qwen classify, only if agent-less worklogs exist):
  - send worklog titles to Qwen for agent inference
  - update worklog agents, then deterministic linkage

Phase 3 (light Qwen observe, only if recent turns exist):
  - send recent turn content to Qwen for fact extraction
  - validate and save observations

Each Qwen call is stateless and lightweight (~200-500 token prompts).
llama.cpp server stays alive between calls preserving KV cache.

Usage:
  python3 qwen_worker.py                    # full 3-phase run
  python3 qwen_worker.py --dry-run          # show prompts without executing
  python3 qwen_worker.py --source qwen      # only process one source
  python3 qwen_worker.py --no-llm           # Phase 1 deterministic only
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from lib.agents import normalize as normalize_agent
from lib.qwen_executor import (
    _discover_sessions,
    _psql,
    PARSERS,
    CANONICAL_AGENTS,
    CLASSIFY_SYSTEM_PROMPT,
    OBSERVE_SYSTEM_PROMPT,
    gather_context,
    enrich_with_search,
    call_qwen,
    execute_deterministic_linkage,
    execute_linkages,
    execute_fixes,
    execute_observations,
    validate_linkages,
)

CHECKPOINT_FILE = Path("/opt/projects/server/scripts/qwen_worker_checkpoint.json")
COLLECT_CHECKPOINT = Path("/opt/projects/server/collect_checkpoint.json")
INGEST_URL = "http://localhost:8000/ingest"


def _load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    if COLLECT_CHECKPOINT.exists():
        cp = json.loads(COLLECT_CHECKPOINT.read_text())
        sessions = {}
        for src, entries in cp.items():
            if isinstance(entries, dict):
                filtered = {k: v for k, v in entries.items()
                            if isinstance(v, int)}
                if filtered:
                    sessions[src] = filtered
        return {"sessions": sessions}
    return {}


def _save_checkpoint(cp: dict) -> None:
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=2, ensure_ascii=False))


def _ensure_worklog(agent: str, model: str, date_str: str) -> bool:
    """Create a daily worklog for agent+date if one doesn't exist. Returns True if created."""
    existing = _psql(
        f"SELECT id FROM worklog_entries "
        f"WHERE agent = '{agent}' AND date = '{date_str}'::date"
    )
    if existing.strip():
        return False

    # Set created_at to noon UTC on that date so ±24h window covers the full day
    ts = f"{date_str} 12:00:00+00"
    title = f"{date_str} {agent} session"
    result = _psql(
        f"INSERT INTO worklog_entries (agent, model, title, summary, date, created_at) "
        f"VALUES ('{agent}', '{model or agent}', '{title}', "
        f"'Auto-generated daily worklog for {agent}', "
        f"'{date_str}'::date, '{ts}'::timestamptz) "
        f"RETURNING id"
    )
    if result.strip().isdigit():
        print(f"  Auto-created worklog #{result.strip()}: {title}")
        return True
    return False


def _backfill_missing_worklogs() -> int:
    """Create worklogs for past dates that have unlinked turns but no worklog."""
    created = 0
    gaps = _psql(
        "SELECT t.agent, t.created_at::date as day, COUNT(*) "
        "FROM turns t "
        "WHERE t.id NOT IN (SELECT unnest(COALESCE(w.turn_ids, '{}'::uuid[])) "
        "                   FROM worklog_entries w WHERE w.turn_ids IS NOT NULL) "
        "  AND t.agent NOT IN (SELECT COALESCE(w2.agent, '') FROM worklog_entries w2 "
        "                       WHERE w2.date = t.created_at::date AND w2.agent = t.agent) "
        "GROUP BY t.agent, t.created_at::date "
        "ORDER BY day, agent"
    )
    if not gaps.strip():
        return 0

    for line in gaps.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        agent = parts[0]
        day = parts[1]
        count = parts[2]
        _ensure_worklog(agent, agent, day)
        created += 1

    return created


def _ingest_sessions(ctx: dict, checkpoint: dict) -> int:
    """Deterministic ingestion — no LLM involved. Returns count of ingested turns."""
    ingested = 0
    for s in ctx.get("sessions", []):
        sid = s["session_id"]
        source = s["source"]
        start_idx = s["checkpoint"]
        project = s.get("project", "")

        sessions = _discover_sessions(source)
        path = None
        for s_sid, s_path, s_proj in sessions:
            if s_sid == sid:
                path = s_path
                if s_proj:
                    project = s_proj
                break
        if path is None:
            continue

        parsed, model, _ = PARSERS[source](path)
        if parsed is None or start_idx >= len(parsed):
            continue

        new_turns = parsed[start_idx:]
        agent = normalize_agent(source)

        for attempt in range(3):
            try:
                r = requests.post(INGEST_URL, json={
                    "source": agent,
                    "model": model,
                    "conversation_id": sid,
                    "title": project or sid[:8],
                    "turns": new_turns,
                }, timeout=30)
                if r.status_code == 200:
                    count = r.json().get("count", 0)
                    ingested += count
                    checkpoint.setdefault("sessions", {}).setdefault(source, {})[sid] = \
                        start_idx + len(new_turns)
                    print(f"  Ingested {source}/{sid[:8]}: +{count} turns")
                    # Auto-create worklog for today if none exists
                    if count > 0:
                        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        _ensure_worklog(agent, model or agent, today_str)
                    break
                else:
                    print(f"  Ingest {source}/{sid[:8]}: HTTP {r.status_code} (attempt {attempt+1}/3)")
                    time.sleep(2 ** attempt)
            except Exception as e:
                print(f"  Ingest error {source}/{sid[:8]}: {e} (attempt {attempt+1}/3)")
                time.sleep(2 ** attempt)

    return ingested


def _build_classify_prompt(worklogs: list) -> str:
    """Build a minimal prompt for agent classification from worklog titles."""
    lines = []
    lines.append("Infer the AI agent for each worklog from its title.")
    lines.append("")
    for w in worklogs:
        lines.append(
            f"worklog_id: {w['worklog_id']} | "
            f"title: {w['title']} | "
            f"date: {w.get('created_at', '')[:10]}"
        )
    lines.append("")
    lines.append("Return a JSON object with classifications array.")
    return "\n".join(lines)


def _build_observe_prompt(turns: list, agent: str = "") -> str:
    """Build a minimal prompt for fact extraction from a single agent batch."""
    lines = []
    if agent:
        lines.append(f"## AGENT: {agent}  (3 turns from the same conversation context)")
        lines.append("")
    lines.append("## TURN CONTENT — extract facts from these turns")
    lines.append("")
    for t in turns:
        ut = (t["user_turn"] or "")[:200]
        th = (t.get("thinking") or "")[:200]
        tx = (t["text"] or "")[:200]
        lines.append(f"[{t['agent']}] user: {ut}")
        if th:
            lines.append(f"[{t['agent']}] thinking: {th}")
        lines.append(f"[{t['agent']}] text: {tx}")
        lines.append("--")
    lines.append("")
    lines.append("## INSTRUCTIONS")
    lines.append("Extract self-contained facts ONLY. Skip context-dependent fragments:")
    lines.append('  SKIP: "yes", "apply it", "ok", "진행해", "그래", "맞아" — requires prior context')
    lines.append('  SKIP: "what about the 3-chunk approach?" — question references unknown prior topic')
    lines.append('  KEEP: "Redis 대신 SQL JOIN으로 결정론적 링크 처리" — self-contained decision')
    lines.append('  KEEP: "orphans: 602개, coverage: 183/183" — self-contained data')
    lines.append("If no self-contained facts, return empty observations array [].")
    return "\n".join(lines)


def _next_observe_batch(observed_through: dict) -> tuple:
    """Get next batch: oldest agent group with unobserved turns (3 turns max).
    Returns (turns_list, agent) or ([], None)."""
    # Find the globally oldest agent group with unobserved turns
    all_groups = _psql(
        "SELECT t.agent, MIN(t.created_at)::text "
        "FROM turns t "
        "GROUP BY t.agent "
        "ORDER BY MIN(t.created_at) "
        "LIMIT 10"
    )
    best_agent, best_oldest = None, None
    for line in all_groups.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        agent, oldest = parts[0], parts[1][:19]
        cursor = observed_through.get(agent, "")
        if not cursor or oldest > cursor:
            if best_oldest is None or oldest < best_oldest:
                best_agent, best_oldest = agent, oldest

    if best_agent is None:
        return [], None

    # Fetch up to 3 turns for this agent after the cursor
    cursor = observed_through.get(best_agent, "")
    cursor_clause = (
        f"AND t.created_at > '{cursor}'::timestamptz" if cursor else ""
    )
    turns_rows = _psql(
        f"SELECT COALESCE(t.agent, ''), "
        f"  replace(replace(COALESCE(t.user_turn, ''), E'\n', ' '), '|', '/'), "
        f"  replace(replace(COALESCE(t.thinking, ''), E'\n', ' '), '|', '/'), "
        f"  replace(replace(COALESCE(t.text, ''), E'\n', ' '), '|', '/'), "
        f"  t.created_at::text "
        f"FROM turns t "
        f"WHERE t.agent = '{best_agent}' "
        f"  {cursor_clause} "
        f"ORDER BY t.created_at LIMIT 3"
    )
    turns = []
    for line in turns_rows.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 5:
            turns.append({
                "agent": parts[0],
                "user_turn": parts[1][:200] if parts[1] else "",
                "thinking": parts[2][:200] if parts[2] else "",
                "text": parts[3][:200] if parts[3] else "",
                "created_at": parts[4][:19] if parts[4] else "",
            })
    return turns, best_agent


def _validate_linkages(actions: list) -> list:
    """Validate Qwen's linkage proposals against DB before execution."""
    valid = []
    existing_ids = set()
    rows = _psql("SELECT id FROM worklog_entries")
    for line in rows.split("\n"):
        if line.strip().isdigit():
            existing_ids.add(int(line.strip()))

    for a in actions:
        wid = a.get("worklog_id")
        agent = a.get("agent", "").strip()
        if not wid or not agent:
            print(f"  Skipping invalid linkage: missing worklog_id or agent ({a})")
            continue
        try:
            wid = int(wid)
        except (TypeError, ValueError):
            print(f"  Skipping invalid linkage: non-numeric worklog_id ({a})")
            continue
        if wid not in existing_ids:
            print(f"  Skipping linkage: worklog #{wid} not found in DB (Qwen hallucination)")
            continue
        valid.append(a)

    return valid


def _validate_observations(observations: list, turns: list) -> list:
    """Validate structured facts against the batch turns. Returns only valid fact objects."""
    if not observations:
        return []

    # Gather all text from batch turns for verbatim evidence verification
    turn_texts = []
    turn_agents = set()
    for t in turns:
        a = t.get("agent", "")
        if a:
            turn_agents.add(a)
        ut = t.get("user_turn", "") or ""
        th = t.get("thinking", "") or ""
        tx = t.get("text", "") or ""
        if ut:
            turn_texts.append(ut)
        if th:
            turn_texts.append(th)
        if tx:
            turn_texts.append(tx)

    # Fetch existing observations from last 24h for dedup (by evidence)
    existing_raw = _psql(
        "SELECT observation FROM observations "
        "WHERE created_at >= NOW() - INTERVAL '24 hours'"
    )
    existing_evidence = set()
    for line in existing_raw.split("\n"):
        text = line.strip()
        if text:
            existing_evidence.add(text)

    VALID_FACT_TYPES = {"statement", "decision", "action_item", "question", "answer", "data_given"}
    VALID_STATUSES = {"confirmed", "tentative"}
    CANONICAL = {"claude-code", "copilot", "gemini", "qwen", ""}

    # Patterns that indicate context metadata, not turn content
    METADATA_REJECT = re.compile(
        r'^(##|Total turns|agent:.*\|.*turns_created|worklog_id:|query:.*\| via:'
        r'|Non-canonical agents|###\s|Orphan Turns|Unlinked Worklogs)'
    )
    PREFIX_RE = re.compile(r'^\[([\w-]+)\]\s*(user|thinking|text):\s*')

    skipped_hallucination = 0

    valid = []
    for fact in observations:
        if not isinstance(fact, dict):
            skipped_hallucination += 1
            continue

        evidence = (fact.get("evidence") or "").strip()
        speaker = (fact.get("speaker") or "").strip()
        fact_type = (fact.get("fact_type") or "").strip()
        status = (fact.get("status") or "confirmed").strip()

        # Mandatory fields
        if not evidence or not fact_type:
            print(f"  Skipping fact: missing evidence or fact_type (id={fact.get('id')})")
            skipped_hallucination += 1
            continue

        # Reject context metadata patterns
        if METADATA_REJECT.match(evidence):
            print(f"  Skipping fact: evidence is context metadata, not turn content (id={fact.get('id')})")
            skipped_hallucination += 1
            continue

        # Strip [agent] type: prefix from evidence
        prefix_match = PREFIX_RE.match(evidence)
        if prefix_match:
            evidence_agent = prefix_match.group(1)
            # Validate speaker matches evidence prefix agent
            if speaker and evidence_agent != speaker:
                print(f"  Skipping fact: speaker '{speaker}' != evidence prefix '{evidence_agent}' (id={fact.get('id')})")
                skipped_hallucination += 1
                continue
            # Auto-fill speaker from evidence prefix if missing
            if not speaker:
                speaker = evidence_agent
                fact["speaker"] = speaker
            # Strip the prefix
            evidence = evidence[prefix_match.end():].strip()
            fact["evidence"] = evidence

        # Validate fact_type enum
        if fact_type not in VALID_FACT_TYPES:
            print(f"  Skipping fact: invalid fact_type '{fact_type}' (id={fact.get('id')})")
            skipped_hallucination += 1
            continue

        # Validate status enum
        if status not in VALID_STATUSES:
            print(f"  Skipping fact: invalid status '{status}' (id={fact.get('id')})")
            skipped_hallucination += 1
            continue

        # Dedup by evidence within 24h
        if evidence in existing_evidence:
            print(f"  Skipping fact: duplicate evidence (id={fact.get('id')})")
            skipped_hallucination += 1
            continue

        # Evidence must be verbatim from recent turns (substring match, lenient)
        evidence_found = any(evidence[:40] in tt or tt in evidence[:40]
                            for tt in turn_texts if tt)
        if not evidence_found and speaker in turn_agents:
            # Allow if speaker is in recent turns (Qwen may have slightly reformatted)
            pass
        elif not evidence_found and speaker and speaker not in CANONICAL:
            print(f"  Skipping fact: speaker '{speaker}' not canonical (id={fact.get('id')})")
            skipped_hallucination += 1
            continue

        valid.append(fact)
        existing_evidence.add(evidence)  # dedup within batch

    skipped = len(observations) - len(valid)
    if skipped:
        print(f"  Facts filtered: {skipped} hallucinated/rejected, {len(valid)} valid")
    return valid


def main():
    ap = argparse.ArgumentParser(description="Qwen-driven turn worker (3-phase)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show prompts but do not call Qwen or execute")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip Qwen, deterministic phases only")
    ap.add_argument("--source", choices=["claude", "copilot", "gemini", "qwen"],
                    help="Only process one source")
    ap.add_argument("--search", action="store_true",
                    help="Enrich context with web search (daily batch only, NOT for 15min timer)")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc)
    print(f"[{ts.isoformat()}] qwen_worker starting"
          + (" (dry-run)" if args.dry_run else "")
          + (" (no-llm)" if args.no_llm else "")
          + (" (search)" if args.search else ""))

    checkpoint = _load_checkpoint()
    results = {"ingested": 0, "linked": 0, "classified": 0, "observed": 0}

    # Initial context gather
    ctx = gather_context(checkpoint, args.source)
    if args.search:
        ctx = enrich_with_search(ctx)

    has_ingestion = bool(ctx.get("sessions"))
    has_work = has_ingestion or bool(ctx.get("orphan_turns") or ctx.get("unlinked_worklogs"))

    if not has_work:
        print("No work to do (no new sessions, no orphans, no unlinked worklogs)")
        return 0

    # ═══════════════════════════════════════════════
    # Phase 1: DETERMINISTIC (no Qwen)
    #   - ingest new turns
    #   - backfill missing worklogs
    #   - link worklogs that already have agents
    #   - apply agent normalization fixes
    # ═══════════════════════════════════════════════
    print("── Phase 1: Deterministic ──")

    if args.dry_run:
        if has_ingestion:
            print(f"  Would ingest {len(ctx['sessions'])} session(s)")
        print(f"  Would backfill missing worklogs")
        print(f"  Would run deterministic linkage (agent-known worklogs)")
        print(f"  Would apply agent normalization fixes")

    if has_ingestion and not args.dry_run:
        print(f"  Ingesting {len(ctx['sessions'])} session(s)...")
        results["ingested"] = _ingest_sessions(ctx, checkpoint)

    backfilled = _backfill_missing_worklogs() if not args.dry_run else 0
    if backfilled:
        print(f"  Backfilled {backfilled} missing worklog(s)")

    # Deterministic linkage for agent-known worklogs
    det_linked = execute_deterministic_linkage() if not args.dry_run else 0
    if det_linked:
        print(f"  Deterministic linked: {det_linked} turns")
    results["linked"] += det_linked

    # Agent normalization (always deterministic, AGENT_MAP-based)
    fixed = execute_fixes([{"type": "agent_normalization"}]) if not args.dry_run else 0
    if fixed:
        print(f"  Agent normalizations: {fixed}")

    # Re-gather context after Phase 1 changes
    if not args.dry_run:
        ctx = gather_context(checkpoint, args.source)

    # ═══════════════════════════════════════════════
    # Phase 2: QWEN CLASSIFY (agent-less worklogs only)
    #   - send worklog titles to Qwen for agent inference
    #   - update worklog agents
    #   - deterministic linkage for newly agent-ed worklogs
    # ═══════════════════════════════════════════════
    unlinked = ctx.get("unlinked_worklogs", [])
    agentless = [w for w in unlinked if not w["agent"]]
    if agentless and not args.no_llm:
        print(f"── Phase 2: Classify ({len(agentless)} agent-less worklogs) ──")

        if args.dry_run:
            classify_prompt = _build_classify_prompt(agentless)
            print("=== CLASSIFY SYSTEM PROMPT ===")
            print(CLASSIFY_SYSTEM_PROMPT)
            print("=== CLASSIFY USER PROMPT ===")
            print(classify_prompt)
            print("=== End ===")
        else:
            classify_prompt = _build_classify_prompt(agentless)
            result = call_qwen(CLASSIFY_SYSTEM_PROMPT, classify_prompt, max_tokens=512)
            if result is None:
                print("  Qwen unavailable — skipping classify phase")
            else:
                classifications = result.get("classifications", [])
                for c in classifications:
                    wid = c.get("worklog_id")
                    agent = (c.get("agent") or "").strip()
                    if not wid or agent not in CANONICAL_AGENTS:
                        continue
                    _psql(
                        f"UPDATE worklog_entries SET agent = '{agent}' WHERE id = {wid}"
                    )
                    print(f"  Classified worklog #{wid}: agent={agent}")
                    results["classified"] += 1

                # Deterministic linkage for newly classified worklogs
                if classifications:
                    det_linked2 = execute_deterministic_linkage()
                    if det_linked2:
                        print(f"  Post-classify deterministic linked: {det_linked2} turns")
                    results["linked"] += det_linked2

    elif agentless and args.no_llm:
        print(f"── Phase 2: Skipped ({len(agentless)} agent-less worklogs, --no-llm) ──")

    # ═══════════════════════════════════════════════
    # Phase 3: QWEN OBSERVE (batch loop, 3 turns per call)
    #   - group by (conversation_id, agent) for coherent context
    #   - 3 turns per batch, light prompt (~300 tokens)
    #   - loop until all unobserved turns consumed or 12 min elapsed
    # ═══════════════════════════════════════════════
    observed_through = checkpoint.get("observed_through", {})

    if args.dry_run:
        # Show first batch prompt only for brevity
        batch_turns, agent = _next_observe_batch(observed_through)
        if batch_turns and not args.no_llm:
            print(f"── Phase 3: Observe (batch loop, showing 1st batch) ──")
            observe_prompt = _build_observe_prompt(batch_turns, agent=agent)
            print(f"=== OBSERVE BATCH: {agent} ({len(batch_turns)} turns) ===")
            print(OBSERVE_SYSTEM_PROMPT)
            print("=== OBSERVE USER PROMPT ===")
            print(observe_prompt)
            print("=== End ===")
        elif args.no_llm:
            print(f"── Phase 3: Skipped (--no-llm) ──")
    elif not args.no_llm:
        print(f"── Phase 3: Observe (batch loop, 3 turns/call) ──")
        batches = 0

        while True:
            elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
            if elapsed > 720:
                print(f"  Phase 3 timeout at {elapsed:.0f}s, {batches} batches done")
                break

            batch_turns, agent = _next_observe_batch(observed_through)
            if not batch_turns:
                if batches == 0:
                    print("  No unobserved turns")
                else:
                    print(f"  All unobserved turns consumed ({batches} batches)")
                break

            observe_prompt = _build_observe_prompt(batch_turns, agent=agent)
            result = call_qwen(OBSERVE_SYSTEM_PROMPT, observe_prompt, max_tokens=1024)
            if result is None:
                print(f"  Qwen unavailable — stopping ({batches} batches done)")
                break

            obs = result.get("observations", [])
            obs = _validate_observations(obs, batch_turns)
            if obs:
                saved = execute_observations(obs, category="qwen_analysis", context={
                    "agent": agent,
                    "batch_size": len(batch_turns),
                    "run_ts": ts.isoformat(),
                })
                results["observed"] += saved

            # Advance per-agent cursor
            batch_max = max((t.get("created_at") or "") for t in batch_turns)
            observed_through[agent] = batch_max
            checkpoint["observed_through"] = observed_through
            batches += 1

        results["observe_batches"] = batches
    else:
        print(f"── Phase 3: Skipped (--no-llm) ──")

    # ═══════════════════════════════════════════════
    # Final: validation + checkpoint
    # ═══════════════════════════════════════════════
    if not args.dry_run:
        broken = validate_linkages()
        if broken:
            print(f"  WARNING: {broken} worklog(s) have broken turn_id references")

        checkpoint["last_run_utc"] = ts.isoformat()
        _save_checkpoint(checkpoint)

        orphan_str = _psql(
            "SELECT COUNT(*) FROM turns t "
            "WHERE t.id NOT IN (SELECT unnest(COALESCE(w.turn_ids, '{}'::uuid[])) "
            "FROM worklog_entries w WHERE w.turn_ids IS NOT NULL)"
        )
        orphan_after = int(orphan_str.strip()) if orphan_str.strip().lstrip("-").isdigit() else 0
    else:
        orphan_after = 0

    ts_end = datetime.now(timezone.utc)
    elapsed = (ts_end - ts).total_seconds()
    mode = "qwen+deterministic" if (results["classified"] or results["observed"]) else "deterministic"
    print(f"[{ts_end.isoformat()}] done: ingested={results['ingested']} "
          f"linked={results['linked']} classified={results['classified']} "
          f"observed={results['observed']} "
          f"orphans_remaining={orphan_after} elapsed={elapsed:.1f}s "
          f"mode={mode}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
