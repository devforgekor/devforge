#!/usr/bin/env python3
"""qwen_worker.py — Qwen-driven turn collector + linker, runs every 15 min.

Ingestion is ALWAYS deterministic (no LLM). Qwen handles only:
  - linkage (matching orphan turns to worklog entries)
  - fixes (agent normalization, etc.)
  - observations (anomalies, patterns)

This split eliminates hallucinated session IDs entirely.

Usage:
  python3 qwen_worker.py                    # full run
  python3 qwen_worker.py --dry-run          # show prompt without executing
  python3 qwen_worker.py --source qwen      # only process one source
  python3 qwen_worker.py --no-llm           # deterministic ingestion only
"""

import argparse
import json
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
    gather_context,
    enrich_with_search,
    call_qwen,
    execute_linkages,
    execute_fixes,
    validate_linkages,
)

CHECKPOINT_FILE = Path("/opt/projects/server/scripts/qwen_worker_checkpoint.json")
COLLECT_CHECKPOINT = Path("/opt/projects/server/collect_checkpoint.json")
INGEST_URL = "http://localhost:8000/ingest"

SYSTEM_PROMPT = (
    "You are the DevForge database maintenance agent. "
    "Ingestion has already been completed deterministically. "
    "Your job is ONLY to propose linkages and fixes.\n"
    "Output ONLY a JSON object with this structure:\n"
    "{\n"
    '  "linkages": [\n'
    '    {"worklog_id": <int>, "agent": "<agent>"}\n'
    "  ],\n"
    '  "fixes": [\n'
    '    {"type": "agent_normalization", "detail": "<what to fix>"}\n'
    "  ],\n"
    '  "observations": ["<observation>"]\n'
    "}\n\n"
    "RULES:\n"
    "- Canonical agent names: claude-code, copilot, gemini, qwen.\n"
    "- Link orphans ONLY when the orphan agent matches a worklog entry agent.\n"
    "  Skip worklogs with empty agent — they cannot be matched.\n"
    "  Time windows are computed automatically (± 24h from worklog created_at).\n"
    "- If an orphan group has no matching worklog entry, mention it in observations.\n"
    "- Agent names in turns must be canonical. Flag non-canonical agents as fixes.\n"
    "- If nothing needs to be done, return empty arrays.\n"
    "- CRITICAL: Only propose actions for items that actually appear in the context above."
)


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


def _build_prompt(ctx: dict) -> str:
    """Format context for Qwen — linkage/fix decisions only (ingestion already done)."""
    lines = []

    uw = ctx.get("unlinked_worklogs", [])
    if uw:
        lines.append("## UNLINKED WORKLOG ENTRIES (last 30 days)")
        for w in uw:
            lines.append(
                f"worklog_id: {w['worklog_id']} | agent: {w['agent']} | "
                f"title: {w['title']} | created_at: {w['created_at']}"
            )
        lines.append("")

    ot = ctx.get("orphan_turns", [])
    if ot:
        lines.append("## ORPHAN TURNS (not linked to any worklog)")
        for o in ot:
            lines.append(
                f"agent: {o['agent']} | count: {o['count']} | "
                f"earliest: {o['earliest']} | latest: {o['latest']}"
            )
        lines.append("")

    recent = ctx.get("recent_activity", {})
    if recent:
        lines.append("## RECENT ACTIVITY (last 6 hours)")
        for agent, count in sorted(recent.items(), key=lambda x: -x[1]):
            lines.append(f"agent: {agent} | turns_created: {count}")
        lines.append("")

    search = ctx.get("search_results", [])
    if search:
        lines.append("## WEB SEARCH RESULTS (for unresolved orphans/unknowns)")
        for s in search:
            lines.append(f"query: {s['query']} | via: {s['source']}")
            for r in s.get("results", []):
                lines.append(f"  - {r['title']}: {r['snippet'][:200]}")
        lines.append("")

    lines.append(f"## TOTAL TURNS IN DB: {ctx.get('total_turns', 0)}")
    lines.append("")

    bad = ctx.get("bad_agents", [])
    if bad:
        lines.append(f"## NON-CANONICAL AGENTS IN DB: {', '.join(bad)}")
        lines.append("")

    lines.append(
        "## INSTRUCTIONS\n"
        "Ingestion has already been completed. Only propose linkages and fixes.\n"
        "Output ONLY a JSON object with linkages, fixes, and observations.\n"
        "If no actions are needed, return empty arrays for all keys."
    )
    return "\n".join(lines)


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


def main():
    ap = argparse.ArgumentParser(description="Qwen-driven turn worker")
    ap.add_argument("--dry-run", action="store_true",
                    help="Show prompt but do not call Qwen or execute")
    ap.add_argument("--no-llm", action="store_true",
                    help="Skip Qwen, deterministic ingestion only")
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

    # Phase 1: Gather context
    ctx = gather_context(checkpoint, args.source)
    if args.search:
        ctx = enrich_with_search(ctx)

    has_ingestion = bool(ctx.get("sessions"))
    has_linking = bool(ctx.get("orphan_turns") or ctx.get("unlinked_worklogs"))
    has_work = has_ingestion or has_linking

    if not has_work:
        print("No work to do (no new sessions, no orphans, no unlinked worklogs)")
        return 0

    results = {"ingested": 0, "linked": 0, "fixed": 0, "mode": "none"}

    if args.dry_run:
        # Show what WOULD happen, don't execute anything
        if has_ingestion:
            print(f"Would ingest {len(ctx['sessions'])} session(s) deterministically")
        if has_linking:
            prompt = _build_prompt(ctx)
            print("=== SYSTEM PROMPT ===")
            print(SYSTEM_PROMPT[:500])
            print("=== USER PROMPT (context) ===")
            print(prompt[:3000])
            print("=== End ===")
        else:
            print("No linkage work for Qwen to analyze")
        return 0

    # Phase 2: Deterministic ingestion (ALWAYS, no LLM)
    if has_ingestion:
        print(f"Ingesting {len(ctx['sessions'])} session(s) deterministically...")
        results["ingested"] = _ingest_sessions(ctx, checkpoint)
        results["mode"] = "deterministic"

    # Phase 2b: Backfill missing worklogs for orphan dates
    backfilled = _backfill_missing_worklogs()
    if backfilled:
        print(f"Backfilled {backfilled} missing worklog(s)")
        # Refresh context so Qwen sees the new worklogs
        ctx = gather_context(checkpoint, args.source)
        has_linking = bool(ctx.get("orphan_turns") or ctx.get("unlinked_worklogs"))

    # Phase 3: Qwen for linkage + fixes (only if orphans/worklogs exist)
    if has_linking and not args.no_llm:
        prompt = _build_prompt(ctx)
        actions = call_qwen(SYSTEM_PROMPT, prompt)

        if actions is None:
            print("Qwen unavailable — skipping linkage phase")
        else:
            linkages = actions.get("linkages", [])
            fixes = actions.get("fixes", [])
            obs = actions.get("observations", [])
            for o in obs:
                print(f"  Qwen: {o}")

            # Validate before execution
            linkages = _validate_linkages(linkages)

            if linkages or fixes:
                print(f"Actions: {len(linkages)} linkages, {len(fixes)} fixes")
                results["linked"] = execute_linkages(linkages)
                results["fixed"] = execute_fixes(fixes)
                results["mode"] = "qwen+deterministic"
            else:
                print("Qwen proposed no valid actions")

    elif has_linking and args.no_llm:
        print("Skipping linkage (--no-llm)")

    # Phase 4: Validate linkage integrity
    broken = validate_linkages()
    if broken:
        print(f"  WARNING: {broken} worklog(s) have broken turn_id references")

    # Phase 5: Save checkpoint
    checkpoint["last_run_utc"] = ts.isoformat()
    _save_checkpoint(checkpoint)

    # Verify
    orphan_str = _psql(
        "SELECT COUNT(*) FROM turns t "
        "WHERE t.id NOT IN (SELECT unnest(COALESCE(w.turn_ids, '{}'::uuid[])) "
        "FROM worklog_entries w WHERE w.turn_ids IS NOT NULL)"
    )
    orphan_after = int(orphan_str.strip()) if orphan_str.strip().lstrip("-").isdigit() else 0

    ts_end = datetime.now(timezone.utc)
    elapsed = (ts_end - ts).total_seconds()
    print(f"[{ts_end.isoformat()}] done: ingested={results['ingested']} "
          f"linked={results['linked']} fixed={results['fixed']} "
          f"orphans_remaining={orphan_after} elapsed={elapsed:.1f}s "
          f"mode={results['mode']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
