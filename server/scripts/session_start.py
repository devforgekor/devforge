#!/usr/bin/env python3
"""SessionStart hook — inject DevForge work context into Claude."""
import json
import subprocess
from pathlib import Path

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
        "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]
TASKS_FILE = Path("/opt/projects/server/docs/tasks.yaml")
COLLECT_STATUS = Path("/opt/projects/server/docs/collect_status.yaml")
REPORT_FILE = Path("/opt/projects/server/docs/consistency_report.yaml")
LINK_REVIEW = Path("/opt/projects/server/docs/link_review.yaml")
SERVER_DIR = Path("/opt/projects/server")

def _psql(sql):
    try:
        r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def _git(cmd):
    try:
        r = subprocess.run(["git"] + cmd, capture_output=True, text=True, timeout=10, cwd=str(SERVER_DIR))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def main():
    lines = ["## DevForge Session Context"]

    # ── DB turn stats ──────────────────────────────────────
    counts = _psql(
        "SELECT c.source, COUNT(*) FROM turns t "
        "JOIN conversations c ON t.conversation_id = c.id "
        "GROUP BY c.source ORDER BY COUNT(*) DESC"
    )
    lines.append("DB turns by source:")
    if counts:
        for row in counts.split("\n"):
            if "|" in row:
                src, cnt = row.split("|", 1)
                lines.append(f"  {src}: {cnt}")
    else:
        lines.append("  (empty)")

    today_turns = _psql("SELECT COUNT(*) FROM turns WHERE created_at::date = CURRENT_DATE")
    lines.append(f"  today: {today_turns or '0'} new")

    lines.append("")

    # ── Collection status ──────────────────────────────────
    if COLLECT_STATUS.exists():
        lines.append("Collection (last run):")
        for raw in COLLECT_STATUS.read_text().strip().split("\n"):
            line = raw.strip()
            if line.startswith("#") or not line:
                continue
            lines.append(f"  {line}")
        lines.append("")

    # ── Link review (nightly) ──────────────────────────────
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
                lines.append(f"## ACTION REQUIRED: {orphan_count} orphan turns detected")
                lines.append(f"  These turns have no worklog entry. AI MUST:")
                lines.append(f"  1. Review orphan turns in DB")
                lines.append(f"  2. Run: python3 /opt/projects/server/scripts/cli.py worklog add '<title>' '<summary>'")
                lines.append(f"  3. Re-run link_turns to match them")
                lines.append(f"  User message: 미완료 작업이 있습니다. 지금 수행합니다.")
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

    # ── Recent worklog ─────────────────────────────────────
    rows = _psql("SELECT date, title FROM worklog_entries ORDER BY created_at DESC LIMIT 5")
    recent_lines = []
    if rows:
        for line in rows.split("\n"):
            if "|" in line:
                date, title = line.split("|", 1)
                recent_lines.append(f"- [{date}] {title}")
    lines.append("Recent worklog (DB):")
    lines.append("\n".join(recent_lines) if recent_lines else "(none)")
    lines.append("")

    # ── Current task ───────────────────────────────────────
    task_line = "(tasks.yaml not found)"
    if TASKS_FILE.exists():
        import yaml
        try:
            tasks = yaml.safe_load(TASKS_FILE.read_text()) or {}
            cur = tasks.get("in_progress")
            if cur and isinstance(cur, dict):
                task_line = f"{cur.get('title', '?')} [{cur.get('date', '?')}]"
            else:
                task_line = "(no in_progress task)"
        except Exception:
            task_line = "(tasks.yaml parse error)"
    lines.append(f"In progress task: {task_line}")
    lines.append("")

    # ── Unlogged sessions ──────────────────────────────────
    auto_commits = _git(["log", "--since=yesterday", "--grep=[auto] unlogged", "--oneline"])
    if auto_commits:
        lines.append("## UNLOGGED SESSIONS (auto-committed by git safety net)")
        for line in auto_commits.split("\n"):
            lines.append(f"  {line}")
        lines.append("ACTION: Review, update tasks.yaml, run worklog add for completed work.")
        lines.append("")

    # ── Consistency warnings ───────────────────────────────
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

    # ── Reference ──────────────────────────────────────────
    lines.append("---")
    lines.append("Entry point → /opt/projects/server/CLAUDE.yaml (rules, entry_points, handover)")
    lines.append("Key docs:")
    lines.append("  design.md    — architecture, containers, network, DB schema")
    lines.append("  blueprint.yaml — phases, target services, roadmap")
    lines.append("  phases.md    — progress tracker (Phase 1 complete, Phase 2 planned)")
    lines.append("  state.yaml   — live system state (storage, containers, metrics)")
    lines.append("  tasks.yaml   — todo/in_progress/blocked/done task tracker")
    lines.append("Worklog: python3 /opt/projects/server/scripts/cli.py worklog add/recent/search")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(lines)
        }
    }
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    main()
