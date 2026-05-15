#!/usr/bin/env python3
"""Session guard — auto-commit unlogged changes so nothing is lost."""
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

RULE_SRC = Path("/home/opc/common-rule.md")
COPILOT_INSTR = Path("/home/opc/.copilot/copilot-instructions.md")
COPILOT_INSTR_HEADER = (
    "# Copilot Instructions\n\n"
    "> 인프라/배포 관련 작업 시 `/home/opc/common-main.md`를 읽어라.\n\n"
    "---\n\n"
)


def _sync_copilot_instructions() -> None:
    """Rebuild copilot-instructions.md from common-rule.md on every session end."""
    try:
        if not RULE_SRC.exists():
            return
        COPILOT_INSTR.parent.mkdir(parents=True, exist_ok=True)
        COPILOT_INSTR.write_text(COPILOT_INSTR_HEADER + RULE_SRC.read_text())
    except Exception as e:
        print(f"[session_guard] copilot-instructions sync failed: {e}", file=sys.stderr)

SERVER = Path("/opt/projects/server")
PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
        "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]
KST = timezone(timedelta(hours=9))

def _psql(sql):
    try:
        r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def _git(args):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=15, cwd=str(SERVER))
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

def main():
    # 1. Any file changes?
    diff = _git(["diff", "--stat", "HEAD"])
    untracked = _git(["ls-files", "--others", "--exclude-standard"])
    if not diff and not untracked:
        return 0  # Clean — nothing to guard

    # 2. Check if work was already recorded
    today = datetime.now(KST).strftime("%Y-%m-%d")

    # Check worklog DB for today's entries
    wl_count = _psql(f"SELECT COUNT(*) FROM worklog_entries WHERE created_at::date = '{today}'")
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
    _sync_copilot_instructions()
    sys.exit(main())
