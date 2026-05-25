#!/usr/bin/env python3
"""DevForge CLI — AI 대화 검색 및 저장 도구."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from lib.agents import normalize as normalize_agent
from lib.db import psql as _sql, esc_sql


def _format_results(rows):
    for r in rows:
        print(f"--- [{r['source']}] {r['title'] or '(no title)'} ---")
        print(f"  conversation: {r['conversation_id']}")
        print(f"  model: {r['model']}  seq: {r['seq']}  created: {r['created_at']}")
        ut = (r["user_turn"] or "")[:120]
        tx = (r["text"] or "")[:120]
        print(f"  user: {ut}")
        print(f"  text: {tx}")
        print()


async def cmd_search(args):
    from api.search import search_memories
    results = await search_memories(query=args.query, source=args.source, limit=args.limit)
    print(f"Found {len(results)} results for '{args.query}'\n")
    _format_results(results)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))


async def cmd_save(args):
    from api.search import save_memory
    detail = {}
    if args.detail:
        try:
            detail = json.loads(args.detail)
        except json.JSONDecodeError:
            detail = {"user_turn": args.detail, "text": args.summary or ""}

    result = await save_memory(
        source=args.source,
        user_turn=detail.get("user_turn", args.detail or ""),
        text=detail.get("text", args.summary or ""),
        title=args.summary,
        model=args.model,
        thinking=detail.get("thinking"),
        meta=detail.get("meta", {}),
    )
    print(json.dumps(result, indent=2))


async def cmd_recent(args):
    from api.search import search_memories
    results = await search_memories(query="", source=args.source, limit=args.limit)
    print(f"Recent {len(results)} entries:\n")
    _format_results(results)


def cmd_worklog_add(args):
    """Insert a new worklog entry directly into PostgreSQL."""
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else []
    details_json = json.dumps(args.details.split(",") if args.details else [])
    files_json = json.dumps(files)
    tags_array = "{" + ",".join(tags) + "}"
    agent_val = esc_sql(normalize_agent(args.agent)) if args.agent else ""
    model_val = esc_sql(args.model) if args.model else ""

    columns = "date, title, summary, details, files, tags, status, kind"
    values = f"CURRENT_DATE, '{esc_sql(args.title)}', '{esc_sql(args.summary)}', '{details_json}'::jsonb, '{files_json}'::jsonb, '{tags_array}', 'done', 'task'"
    if agent_val:
        columns += ", agent"
        values += f", '{agent_val}'"
    if model_val:
        columns += ", model"
        values += f", '{model_val}'"

    sql = f"INSERT INTO worklog_entries ({columns}) VALUES ({values}) RETURNING id"
    result = _sql(sql)
    if not result or not result.strip().isdigit():
        return
    print(f"+ {args.title[:60]}")
    if agent_val:
        print(f"  agent: {agent_val}")
    if args.model:
        print(f"  model: {args.model}")
    if tags:
        print(f"  tags: {', '.join(tags)}")


def cmd_worklog_recent(args):
    """Show recent worklog entries."""
    limit = args.limit or 3
    sql = f"SELECT date, title, summary, tags, agent, model FROM worklog_entries ORDER BY created_at DESC LIMIT {limit}"
    result = _sql(sql)
    if not result:
        return
    for line in result.split("\n"):
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) < 3:
            continue
        date, title, summary = parts[0], parts[1], parts[2]
        tags_str = parts[3] if len(parts) > 3 else ""
        agent_str = parts[4] if len(parts) > 4 else ""
        model_str = parts[5] if len(parts) > 5 else ""
        header = f"[{date}] {title}"
        if agent_str:
            header += f"  ({agent_str}"
            if model_str:
                header += f"/{model_str}"
            header += ")"
        print(header)
        print(f"  {summary[:120]}")
        if tags_str and tags_str != "{}":
            print(f"  tags: {tags_str}")
        print()


def cmd_worklog_search(args):
    """Search worklog entries by keyword or tag."""
    conditions = []
    if args.tag:
        tag_esc = esc_sql(args.tag)
        conditions.append(f"tags @> '{{{tag_esc}}}'")
    if args.query:
        query_esc = esc_sql(args.query)
        conditions.append(f"(title ILIKE '%{query_esc}%' OR summary ILIKE '%{query_esc}%')")

    where = " AND ".join(conditions) if conditions else "TRUE"
    limit = args.limit or 20
    sql = f"SELECT date, title, summary, tags, agent, model FROM worklog_entries WHERE {where} ORDER BY created_at DESC LIMIT {limit}"
    result = _sql(sql)
    if not result:
        return

    count = 0
    for line in result.split("\n"):
        if not line:
            continue
        parts = line.split("|", 5)
        if len(parts) < 3:
            continue
        date, title, summary = parts[0], parts[1], parts[2]
        tags_str = parts[3] if len(parts) > 3 else ""
        agent_str = parts[4] if len(parts) > 4 else ""
        model_str = parts[5] if len(parts) > 5 else ""
        header = f"[{date}] {title}"
        if agent_str:
            header += f"  ({agent_str}"
            if model_str:
                header += f"/{model_str}"
            header += ")"
        print(header)
        print(f"  {summary[:120]}")
        if tags_str and tags_str != "{}":
            print(f"  tags: {tags_str}")
        print()
        count += 1
    print(f"{count} results")


def cmd_activity_recent(args):
    """Show recent activity_log entries."""
    limit = getattr(args, "limit", 10)
    today = getattr(args, "today", False)
    where = "WHERE created_at::date = CURRENT_DATE" if today else ""
    sql = f"SELECT id, created_at, type, source, title, summary_status, queue_status FROM activity_log {where} ORDER BY created_at DESC LIMIT {limit}"
    rows = _sql(sql).split("\n") if _sql(sql) else []
    print(f"{'ID':<6} {'Created':<20} {'Type':<10} {'Source':<14} {'Title':<50} {'Sum.Status':<12} {'Q.Status'}")
    print("-" * 130)
    for row in rows:
        if not row.strip():
            continue
        parts = row.split("|", 6)
        if len(parts) >= 7:
            print(f"{parts[0]:<6} {parts[1]:<20} {parts[2]:<10} {parts[3]:<14} {parts[4][:48]:<50} {parts[5]:<12} {parts[6]}")


def cmd_activity_stats(args):
    """Show activity_log counts by type and status."""
    sql = """SELECT type, summary_status, count(*) FROM activity_log
             WHERE created_at > NOW() - INTERVAL '7 days'
             GROUP BY type, summary_status ORDER BY type, summary_status"""
    rows = _sql(sql).split("\n") if _sql(sql) else []
    print(f"{'Type':<12} {'Status':<14} {'Count'}")
    print("-" * 40)
    for row in rows:
        if not row.strip():
            continue
        parts = row.split("|")
        if len(parts) >= 3:
            print(f"{parts[0]:<12} {parts[1]:<14} {parts[2]}")


def cmd_activity_add(args):
    """Insert a manual entry into activity_log."""
    title = esc_sql(args.title)
    summary = esc_sql(getattr(args, "summary", ""))
    tags = getattr(args, "tags", "")
    tags_sql = "ARRAY[" + ",".join(f"'{esc_sql(t.strip())}'" for t in tags.split(",") if t.strip()) + "]" if tags else "'{}'"
    result = _sql(f"""INSERT INTO activity_log (type, source, title, summary, tags, summary_status)
        VALUES ('manual', 'cli', '{title}', '{summary}', {tags_sql}, 'raw') RETURNING id""")
    if result and result.strip():
        print(f"  Added: {title} (id={result.strip()})")
    else:
        print("  Failed to add entry")


MODE_FILE = "/opt/ai_data/scripts/current-mode.env"

def _switch_mode(mode: str) -> bool:
    """Write mode file and restart affected containers. Returns True on success."""
    current = ""
    if os.path.exists(MODE_FILE):
        with open(MODE_FILE) as f:
            current = f.read().strip()
    if current == f"MODE={mode}":
        print(f"Already in {mode} mode")
        return True

    with open(MODE_FILE, "w") as f:
        f.write(f"MODE={mode}\n")
    print(f"Switched to {mode} mode")

    # Restart the swap container (Podman B) to pick up new mode
    print("Restarting container-devforge-swap...")
    r = subprocess.run(
        ["systemctl", "--user", "restart", "container-devforge-swap"],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        print(f"Error restarting container-devforge-swap: {r.stderr}")
        return False

    # Handle Podman A: stop for code (no memory), restart otherwise
    if mode == "code":
        print("Stopping container-devforge-qwen (Podman A, not needed in code)...")
        subprocess.run(
            ["systemctl", "--user", "stop", "container-devforge-qwen"],
            capture_output=True, text=True, timeout=30,
        )
    else:
        print("Restarting container-devforge-qwen (Podman A)...")
        r = subprocess.run(
            ["systemctl", "--user", "restart", "container-devforge-qwen"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            print(f"Warning: container-devforge-qwen restart: {r.stderr}")

    # Wait for model to load (Phi-4 14B takes ~60-120s)
    print("Waiting for models to load...")
    for _ in range(120):
        try:
            req = urllib.request.Request("http://127.0.0.1:8081/health")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
                    if data.get("status") == "ok":
                        print(f"  Ready: {data.get('slots_idle', '?')} idle / {data.get('slots_processing', '?')} processing")
                        return True
        except Exception:
            pass
        time.sleep(2)
    print("Warning: model health check timed out, may still be loading")
    return True


def cmd_discussion(args):
    """Launch multi-agent LLM debate (DRAG or Tool-MAD)."""
    method = args.method  # "drag" or "toolmad"
    question = getattr(args, "question", None)
    skip_drag = getattr(args, "skip_drag", False)
    dry_run = getattr(args, "dry_run", False)
    with_api = getattr(args, "with_api", False)

    # Ensure container is in discussion mode (debate-supervisor)
    if not _container_in_discussion_mode():
        print("Switching container to discussion mode...")
        if not _switch_mode("discussion"):
            print("ERROR: failed to switch to discussion mode")
            return

    # Prompt for question if not provided
    if not question:
        try:
            question = input("Enter your question: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            return
    if not question:
        print("ERROR: question required")
        return

    print(f"\nStarting debate [{method.upper()}]...")
    print(f"  Question: {question}")
    if skip_drag:
        print(f"  DRAG: skipped")
    if dry_run:
        print(f"  Dry-run: enabled")

    # Import and run
    from debate import DebateSession
    session = DebateSession(
        question=question,
        method=method,
        mode="discussion",
        skip_drag=skip_drag,
        dry_run=dry_run,
        with_api=with_api,
    )
    result = session.run_session()
    if result is None:
        print("\nDebate FAILED — check logs above for details.")
    else:
        print(f"\nReport: /opt/ai_data/debate_sessions/{session.session_id}/final_report.md")


def _container_in_discussion_mode() -> bool:
    """Check if devforge-swap container is running in debate mode."""
    try:
        r = subprocess.run(
            ["podman", "exec", "devforge-swap", "pgrep", "-f", "debate-supervisor"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def cmd_upload(args):
    """Upload a pipeline result or raw file to Azure Blob."""
    pipeline = args.pipeline
    session_id = getattr(args, "session_id", None)
    file_path = getattr(args, "file", None)
    title = getattr(args, "title", None)

    if not session_id:
        print("ERROR: --session-id is required")
        return

    from lib.blob_uploader import upload_review_bundle, upload_raw

    if file_path:
        # Raw file upload
        path = Path(file_path)
        if not path.exists():
            print(f"ERROR: file not found: {file_path}")
            return
        content = path.read_text()
        url = upload_raw(content, pipeline, session_id, path.name)
        print(f"Uploaded raw: {path.name}")
    else:
        # Auto-generate review-bundle from known output locations
        content = _find_pipeline_output(pipeline, session_id)
        if content is None:
            print(f"ERROR: no output found for {pipeline}/{session_id}")
            print(f"  Use --file to upload a specific file")
            return
        url = upload_review_bundle(
            content=content,
            pipeline=pipeline,
            session_id=session_id,
            metadata={"title": title} if title else None,
        )
        print(f"Uploaded review-bundle")

    print(f"  Pipeline: {pipeline}")
    print(f"  Session:  {session_id}")
    print(f"  SAS URL:  {url}")


def _find_pipeline_output(pipeline: str, session_id: str) -> Optional[str]:
    """Auto-find pipeline output for review-bundle generation."""
    candidates = {
        "debate": [
            f"/opt/ai_data/debate_sessions/{session_id}/final_report.md",
        ],
        "code_mod": [
            f"/var/tmp/code_mod_tests/local32b_task{session_id}_*.json",
        ],
    }
    patterns = candidates.get(pipeline, [])
    for pattern in patterns:
        import glob as _glob
        for p in sorted(_glob.glob(pattern)):
            return Path(p).read_text()
    return None


def cmd_dashboard(args):
    """Show review_facts model performance dashboard."""
    sql_model = """
SELECT extract_model,
  count(*) AS total,
  count(*) FILTER (WHERE verdict='valid') AS valid,
  count(*) FILTER (WHERE verdict='hallucinated') AS halluc,
  count(*) FILTER (WHERE verdict='context_dependent') AS ctx,
  round(avg(gen_rate)::numeric, 2) AS rate,
  round(avg(elapsed_ms/1000.0)::numeric, 1) AS secs,
  round(avg(cache_hit)::numeric, 0) AS cache_pct
FROM review_facts
GROUP BY extract_model ORDER BY total DESC
"""
    result = _sql(sql_model)
    if not result:
        print("데이터 없음")
        return
    print("═══ Model Performance Dashboard ═══\n")
    print(f"{'Model':<32} {'Total':>5} {'Valid%':>7} {'Halluc%':>8} {'Rate':>6} {'Sec':>5} {'Cache%':>6}")
    print("─" * 78)
    for line in result.split("\n"):
        if not line:
            continue
        p = line.split("|")
        if len(p) < 8:
            continue
        model = p[0].strip()[:30]
        total = int(p[1].strip() or 0)
        valid = int(p[2].strip() or 0)
        halluc = int(p[3].strip() or 0)
        valid_pct = f"{valid/total*100:.1f}" if total else "-"
        halluc_pct = f"{halluc/total*100:.1f}" if total else "-"
        rate = p[5].strip() or "-"
        secs = p[6].strip() or "-"
        cache = p[7].strip() or "-"
        print(f"{model:<32} {total:>5} {valid_pct:>7} {halluc_pct:>8} {rate:>6} {secs:>5} {cache:>6}")

    sql_daily = """
SELECT created_at::date AS day,
  count(*) AS total,
  count(*) FILTER (WHERE verdict='valid') AS valid,
  round(avg(gen_rate)::numeric, 2) AS rate
FROM review_facts
GROUP BY day ORDER BY day DESC LIMIT 7
"""
    daily = _sql(sql_daily)
    if daily:
        print(f"\n{'─' * 78}")
        print(f"\n{'Date':<12} {'Facts':>6} {'Valid%':>7} {'Rate(t/s)':>10}")
        print("─" * 40)
        for line in daily.split("\n"):
            if not line:
                continue
            p = line.split("|")
            if len(p) < 4:
                continue
            day = p[0].strip()
            total = int(p[1].strip() or 0)
            valid = int(p[2].strip() or 0)
            rate = p[3].strip() or "-"
            valid_pct = f"{valid/total*100:.1f}" if total else "-"
            print(f"{day:<12} {total:>6} {valid_pct:>7} {rate:>10}")

    sql_summary = """
SELECT count(*), count(*) FILTER (WHERE verdict='valid'),
  count(DISTINCT turn_id), count(DISTINCT extract_model)
FROM review_facts
"""
    s = _sql(sql_summary)
    if s:
        p = s.strip().split("|")
        if len(p) >= 4:
            total, valid, turns, models = int(p[0]), int(p[1]), int(p[2]), int(p[3])
            print(f"\n총 {total} facts / {turns} turns / {models} models — overall valid {valid/total*100:.1f}%")


async def main():
    parser = argparse.ArgumentParser(description="DevForge CLI")
    sub = parser.add_subparsers(dest="command")

    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query")
    p_search.add_argument("--source", "-s", help="Filter by source (e.g., claude, copilot, gemini)")
    p_search.add_argument("--limit", "-n", type=int, default=20)
    p_search.add_argument("--json", "-j", action="store_true")

    p_save = sub.add_parser("save", help="Save a memory")
    p_save.add_argument("--source", "-s", required=True)
    p_save.add_argument("--summary", "-m", help="Title/summary")
    p_save.add_argument("--detail", "-d", help="Detail as JSON string")
    p_save.add_argument("--model", help="AI model name")

    p_recent = sub.add_parser("recent", help="Show recent entries")
    p_recent.add_argument("--source", "-s")
    p_recent.add_argument("--limit", "-n", type=int, default=10)

    p_worklog = sub.add_parser("worklog", help="Worklog management")
    wl_sub = p_worklog.add_subparsers(dest="wl_command")

    wl_add = wl_sub.add_parser("add", help="Add a new worklog entry to DB")
    wl_add.add_argument("title", help="Entry title")
    wl_add.add_argument("summary", help="Entry summary")
    wl_add.add_argument("--tags", help="Comma-separated tags")
    wl_add.add_argument("--files", help="Comma-separated file paths")
    wl_add.add_argument("--details", help="Comma-separated detail items")
    wl_add.add_argument("--agent", help="AI agent name (e.g., claude-code)")
    wl_add.add_argument("--model", help="AI model name (e.g., claude-opus-4-7)")

    wl_recent = wl_sub.add_parser("recent", help="Show recent worklog entries")
    wl_recent.add_argument("--limit", "-n", type=int, default=3)

    wl_search = wl_sub.add_parser("search", help="Search worklog entries")
    wl_search.add_argument("query", nargs="?", help="Keyword to search")
    wl_search.add_argument("--tag", "-t", help="Filter by tag")
    wl_search.add_argument("--limit", "-n", type=int, default=20)

    p_disc = sub.add_parser("discussion", help="Start multi-agent LLM debate (DRAG or Tool-MAD)")
    disc_sub = p_disc.add_subparsers(dest="method")
    drag_p = disc_sub.add_parser("drag", help="DRAG: 2-stage debate — query consensus → fetch → synthesize")
    drag_p.add_argument("question", nargs="?", help="Debate topic / question")
    drag_p.add_argument("--skip-drag", action="store_true", help="Skip Round 0 DRAG query consensus")
    drag_p.add_argument("--dry-run", action="store_true", help="Simulate without LLM calls")
    drag_p.add_argument("--with-api", action="store_true", help="Use DeepSeek API for real search (internet knowledge)")
    tmad_p = disc_sub.add_parser("toolmad", help="Tool-MAD: adaptive real-time search during debate rounds")
    tmad_p.add_argument("question", nargs="?", help="Debate topic / question")
    tmad_p.add_argument("--skip-drag", action="store_true", help="Skip Round 0 DRAG query consensus")
    tmad_p.add_argument("--dry-run", action="store_true", help="Simulate without LLM calls")
    tmad_p.add_argument("--with-api", action="store_true", help="Use DeepSeek API for real search (internet knowledge)")

    sub.add_parser("dashboard", help="Model performance dashboard")

    p_upload = sub.add_parser("upload", help="Upload pipeline result to Azure Blob")
    p_upload.add_argument("--pipeline", "-p", required=True,
                          choices=["debate", "code_mod", "extract"],
                          help="Pipeline name")
    p_upload.add_argument("--session-id", "-s", required=True,
                          help="Session identifier")
    p_upload.add_argument("--file", "-f",
                          help="Raw file path to upload (skip review-bundle wrapping)")
    p_upload.add_argument("--title", "-t",
                          help="Bundle title (for review-bundle mode)")

    p_act = sub.add_parser("activity", help="Activity log management")
    act_sub = p_act.add_subparsers(dest="act_command")

    act_recent = act_sub.add_parser("recent", help="Show recent activity_log entries")
    act_recent.add_argument("--limit", "-n", type=int, default=10)
    act_recent.add_argument("--today", action="store_true", help="Today only")

    act_stats = act_sub.add_parser("stats", help="Activity log statistics")

    act_add = act_sub.add_parser("add", help="Add a manual activity entry")
    act_add.add_argument("title", help="Entry title")
    act_add.add_argument("summary", help="Entry summary")
    act_add.add_argument("--tags", help="Comma-separated tags")

    args = parser.parse_args()

    if args.command == "search":
        await cmd_search(args)
    elif args.command == "save":
        await cmd_save(args)
    elif args.command == "recent":
        await cmd_recent(args)
    elif args.command == "worklog":
        if args.wl_command == "add":
            cmd_worklog_add(args)
        elif args.wl_command == "recent":
            cmd_worklog_recent(args)
        elif args.wl_command == "search":
            cmd_worklog_search(args)
        else:
            p_worklog.print_help()
    elif args.command == "activity":
        if args.act_command == "recent":
            cmd_activity_recent(args)
        elif args.act_command == "stats":
            cmd_activity_stats(args)
        elif args.act_command == "add":
            cmd_activity_add(args)
        else:
            p_act.print_help()
    elif args.command == "discussion":
        if args.method in ("drag", "toolmad"):
            cmd_discussion(args)
        else:
            p_disc.print_help()
    elif args.command == "upload":
        cmd_upload(args)
    elif args.command == "dashboard":
        cmd_dashboard(args)
    else:
        parser.print_help()

    if args.command in ("search", "save", "recent"):
        from api.async_pg import close_pool
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
