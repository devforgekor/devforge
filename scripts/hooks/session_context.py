#!/usr/bin/env python3
# Status: production
# Path: hooks/session_context.py — Claude SessionStart hook (dormant, not in active hooks config)
"""SessionStart hook — inject DevForge work context into Claude."""
import os
import json
import subprocess
from pathlib import Path

from lib.db import psql, psql_json
COLLECT_STATUS = Path("/opt/projects/server/data/collect_status.yaml")
REPORT_FILE = Path("/opt/projects/server/data/consistency_report.yaml")
LINK_REVIEW = Path("/opt/projects/server/data/link_review.yaml")
SERVER_DIR = Path("/opt/projects/server")

def _git(cmd):
    try:
        r = subprocess.run(["git"] + cmd, capture_output=True, text=True, timeout=10, cwd=str(SERVER_DIR))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def main():
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    if "deepseek.com/anthropic" in base_url or "44777" in base_url:
        return

    lines = ["## DevForge Session Context"]

    counts = psql_json(
        "SELECT c.source, COUNT(*) AS cnt FROM turns t "
        "JOIN conversations c ON t.conversation_id = c.id "
        "GROUP BY c.source ORDER BY COUNT(*) DESC"
    )
    lines.append("DB turns by source:")
    if counts:
        for row in counts:
            lines.append(f"  {row['source']}: {row['cnt']}")
    else:
        lines.append("  (empty)")

    today_turns = psql("SELECT COUNT(*) FROM turns WHERE created_at::date = CURRENT_DATE")
    lines.append(f"  today: {today_turns or '0'} new")

    lines.append("")

    from lib.db import get_token_stats
    stats = get_token_stats()
    lines.append("Token status:")
    if stats:
        lines.append(
            f"  session: {stats['session']['turns']} turns / {stats['session']['total_tokens']:,} tokens / avg {stats['session']['avg_tokens']:,} per turn"
        )
        lines.append(
            f"  total: {stats['total']['turns']} turns / {stats['total']['total_tokens']:,} tokens / avg {stats['total']['avg_tokens']:,} per turn"
        )
    else:
        lines.append("  session: no token data")
        lines.append("  total: no token data")

    lines.append("")

    if COLLECT_STATUS.exists():
        lines.append("Collection (last run):")
        for raw in COLLECT_STATUS.read_text().strip().split("\n"):
            line = raw.strip()
            if line.startswith("#") or not line:
                continue
            lines.append(f"  {line}")
        lines.append("")

    if LINK_REVIEW.exists():
        import yaml
        try:
            review_text = LINK_REVIEW.read_text().strip()
            orphan_count = 0
            for raw in review_text.split("\n"):
                line = raw.strip()
                if line.startswith("finding: orphan_turns."):
                    try:
                        orphan_count += int(line.split(":")[-1].strip())
                    except ValueError:
                        pass

            if orphan_count > 0:
                lines.append(f"## ORPHAN TURNS: {orphan_count}")
                lines.append(f"  Turns without worklog — nightly batch will reconcile.")
                lines.append("")

            lines.append("Link review (last nightly):")
            for raw in review_text.split("\n"):
                line = raw.strip()
                if line.startswith("#") or not line:
                    continue
                lines.append(f"  {line}")
            lines.append("")
        except Exception:
            pass

    rows = psql_json("SELECT date, title FROM worklog_entries ORDER BY created_at DESC LIMIT 5")
    recent_lines = []
    if rows:
        for row in rows:
            recent_lines.append(f"- [{row['date']}] {row['title']}")
    lines.append("Recent worklog (DB):")
    lines.append("\n".join(recent_lines) if recent_lines else "(none)")
    lines.append("")

    task_line = "(no in_progress task)"
    from lib.db import psql_json
    tasks = psql_json("SELECT title, status FROM tasks WHERE status = 'in_progress' LIMIT 1")
    if tasks and len(tasks) > 0:
        task_line = tasks[0].get("title", "?")
    lines.append(f"In progress task: {task_line}")
    lines.append("")

    auto_commits = _git(["log", "--since=yesterday", "--grep=[auto] unlogged", "--oneline"])
    if auto_commits:
        lines.append("## UNLOGGED SESSIONS (auto-committed by git safety net)")
        for line in auto_commits.split("\n"):
            lines.append(f"  {line}")
        lines.append("ACTION: Review, update tasks via cli.py task update, run worklog add for completed work.")
        lines.append("")

    if REPORT_FILE.exists():
        import yaml
        try:
            report = yaml.safe_load(REPORT_FILE.read_text()) or {}
            warns = report.get("warnings", [])
            if warns:
                lines.append("## Consistency Warnings (NEEDS FIX)")
                for w in warns[:5]:
                    lines.append(f"- {w}")
                lines.append("")
        except Exception:
            pass

    lines.append("---")
    lines.append("Entry point → /opt/projects/server/CLAUDE.yaml (rules, entry_points, handover)")
    lines.append("Key docs:")
    lines.append("  cli.py status --json — live state (containers, models, timers, services, resources)")
    lines.append("  infra.md — project infrastructure overview")
    lines.append("  blueprint.yaml — phases, target services, roadmap")
    lines.append("  phases.md    — progress tracker (Phase 1 complete, Phase 2 planned)")
    lines.append("  state.yaml   — live system state (storage, containers, metrics)")
    lines.append("  tasks DB (cli.py task list) — todo/in_progress/blocked/done task tracker")
    lines.append("Worklog: auto-generated by turn_watcher + lib/worklog_generator")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines)
        }
    }
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    main()
