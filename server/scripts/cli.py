#!/usr/bin/env python3
"""DevForge CLI — AI 대화 검색 및 저장 도구."""

import argparse
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, "/opt/projects/server")
sys.path.insert(0, "/opt/projects/server/scripts")

from lib.agents import normalize as normalize_agent

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres", "-d", "devforge_app",
        "--no-align", "--tuples-only", "--quiet"]


def _format_results(rows):
    for r in rows:
        print(f"--- [{r['source']}] {r['title'] or '(no title)'} ---")
        print(f"  conversation: {r['conversation_id']}")
        print(f"  model: {r['model']}  seq: {r['seq']}  created: {r['created_at']}")
        q = (r["user_query"] or "")[:120]
        a = (r["assistant_answer"] or "")[:120]
        print(f"  Q: {q}")
        print(f"  A: {a}")
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
            detail = {"user_query": args.detail, "assistant_answer": args.summary or ""}

    result = await save_memory(
        source=args.source,
        user_query=detail.get("user_query", args.detail or ""),
        assistant_answer=detail.get("assistant_answer", args.summary or ""),
        title=args.summary,
        model=args.model,
        reasoning=detail.get("reasoning"),
        meta=detail.get("meta", {}),
    )
    print(json.dumps(result, indent=2))


async def cmd_recent(args):
    from api.search import search_memories
    results = await search_memories(query="", source=args.source, limit=args.limit)
    print(f"Recent {len(results)} entries:\n")
    _format_results(results)


def _psql(sql: str) -> subprocess.CompletedProcess:
    return subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=10)


def _escape_sql(value: str) -> str:
    return value.replace("'", "''").replace("\\", "\\\\")


def cmd_worklog_add(args):
    """Insert a new worklog entry directly into PostgreSQL."""
    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    files = [f.strip() for f in args.files.split(",") if f.strip()] if args.files else []
    details_json = json.dumps(args.details.split(",") if args.details else [])
    files_json = json.dumps(files)
    tags_array = "{" + ",".join(tags) + "}"
    agent_val = _escape_sql(normalize_agent(args.agent)) if args.agent else ""
    model_val = _escape_sql(args.model) if args.model else ""

    columns = "date, title, summary, details, files, tags, status, kind"
    values = f"CURRENT_DATE, '{_escape_sql(args.title)}', '{_escape_sql(args.summary)}', '{details_json}'::jsonb, '{files_json}'::jsonb, '{tags_array}', 'done', 'task'"
    if agent_val:
        columns += ", agent"
        values += f", '{agent_val}'"
    if model_val:
        columns += ", model"
        values += f", '{model_val}'"

    sql = f"INSERT INTO worklog_entries ({columns}) VALUES ({values}) RETURNING id"
    proc = _psql(sql)
    if proc.returncode != 0:
        print(f"DB error: {proc.stderr.strip()}", file=sys.stderr)
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
    proc = _psql(sql)
    if proc.returncode != 0:
        print(f"DB error: {proc.stderr.strip()}", file=sys.stderr)
        return
    for line in proc.stdout.strip().split("\n"):
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
        tag_esc = _escape_sql(args.tag)
        conditions.append(f"tags @> '{{{tag_esc}}}'")
    if args.query:
        query_esc = _escape_sql(args.query)
        conditions.append(f"(title ILIKE '%{query_esc}%' OR summary ILIKE '%{query_esc}%')")

    where = " AND ".join(conditions) if conditions else "TRUE"
    limit = args.limit or 20
    sql = f"SELECT date, title, summary, tags, agent, model FROM worklog_entries WHERE {where} ORDER BY created_at DESC LIMIT {limit}"
    proc = _psql(sql)
    if proc.returncode != 0:
        print(f"DB error: {proc.stderr.strip()}", file=sys.stderr)
        return

    count = 0
    for line in proc.stdout.strip().split("\n"):
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
    else:
        parser.print_help()

    if args.command in ("search", "save", "recent"):
        from api.db import close_pool
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
