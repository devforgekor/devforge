#!/usr/bin/env python3
"""Session guard — auto-commit unlogged changes so nothing is lost.

Also provides log_commits_to_worklog() — called by review_worker.py to record
git commits into worklog_entries with idempotent INSERT ON CONFLICT DO NOTHING.
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.db import psql, psql_ok, esc_sql

SERVER = Path("/opt/projects/server")
KST = timezone(timedelta(hours=9))

def _git(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=15, cwd=str(SERVER))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def log_commits_to_worklog():
    """Scan git log for commits in last 24h, INSERT to worklog_entries.

    Idempotent: ON CONFLICT (date, git_commit_hash) DO NOTHING.
    Returns count of newly recorded commits.
    """
    commits = _git(["log", "--since=24 hours ago", "--format=%H|%s|%an|%aI"])
    if not commits:
        return 0

    saved = 0
    for line in commits.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|", 4)
        if len(parts) < 4:
            continue
        sha = parts[0].strip()
        message = parts[1].strip()
        author = parts[2].strip()
        date_str = parts[3].strip()[:10] if len(parts) > 3 else datetime.now(KST).strftime("%Y-%m-%d")

        title = esc_sql(message[:200])
        summary = esc_sql(message[:500])
        agent_name = esc_sql(author)
        sha_esc = esc_sql(sha)

        result = psql(
            f"INSERT INTO worklog_entries (date, title, summary, git_commit_hash, agent, kind) "
            f"VALUES ('{date_str}', '{title}', '{summary}', '{sha_esc}', '{agent_name}', 'git') "
            f"ON CONFLICT (date, git_commit_hash) DO NOTHING "
            f"RETURNING id"
        )
        if result.strip().isdigit():
            saved += 1

    if saved:
        print(f"  log_commits: {saved} new commit(s) recorded")
    return saved

def main():
    # 1. Any file changes?
    diff = _git(["diff", "--stat", "HEAD"])
    untracked = _git(["ls-files", "--others", "--exclude-standard"])
    if not diff and not untracked:
        return 0  # Clean — nothing to guard

    # 2. Check if work was already recorded
    today = datetime.now(KST).strftime("%Y-%m-%d")

    # Check worklog DB for today's entries
    wl_count = psql(f"SELECT COUNT(*) FROM worklog_entries WHERE created_at::date = '{today}'")
    has_worklog = wl_count and wl_count != "0"

    # Check tasks.yaml mtime
    tasks_file = SERVER / "docs" / "tasks.yaml"
    has_tasks = tasks_file.exists() and datetime.fromtimestamp(tasks_file.stat().st_mtime, tz=KST).strftime("%Y-%m-%d") == today

    # Check handover.yaml mtime
    handover_file = SERVER / "handover.yaml"
    has_handover = handover_file.exists() and datetime.fromtimestamp(handover_file.stat().st_mtime, tz=KST).strftime("%Y-%m-%d") == today

    if has_worklog or has_tasks or has_handover:
        return 0  # Session was logged — AI followed the rules

    # 3. No record found → auto-commit safety net
    _git(["add", "-A"])
    ts = datetime.now(KST).strftime("%Y-%m-%dT%H:%M")
    result = _git(["commit", "-m", f"[auto] unlogged session {ts}"])

    if result:
        # Write warning for next session
        report_file = SERVER / "docs" / "consistency_report.yaml"
        import yaml
        report = {}
        if report_file.exists():
            report = yaml.safe_load(report_file.read_text()) or {}
        warns = report.get("warnings", [])
        warns.append(f"[{today}] unlogged session auto-committed: {result.split(chr(10))[0]}")
        report["warnings"] = warns[-10:]  # Keep last 10
        report["timestamp"] = datetime.now(KST).isoformat()
        report_file.write_text(yaml.dump(report, default_flow_style=False, allow_unicode=True, sort_keys=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
