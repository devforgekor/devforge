#!/usr/bin/env python3
"""update_handover.py — Mechanical session context capture.

Triggered by SessionEnd hook AND 10-min checkpoint timer.
Captures file changes and system fingerprint. AI writes decisions/tasks inline.
"""

import fcntl
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

SERVER_DIR = Path("/opt/projects/server")
HANDOVER_FILE = SERVER_DIR / "handover.yaml"
LOCK_FILE = SERVER_DIR / ".handover.lock"
PROJECT_DIRS = [
    Path("/opt/projects/seedling"),
    Path("/opt/projects/common-lib"),
    Path("/opt/projects/server"),
]
TZ = timezone(timedelta(hours=9))
CHECKPOINT_WINDOW_HOURS = 1  # scan files modified within this window


def _run(cmd, timeout=15, cwd="/opt/projects/seedling"):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.stdout.strip()
    except Exception:
        return ""


def load_handover():
    if HANDOVER_FILE.exists():
        with open(HANDOVER_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_handover(data):
    HANDOVER_FILE.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


def scan_recent_files():
    """Find files modified in the last CHECKPOINT_WINDOW_HOURS."""
    recent = []
    for proj in PROJECT_DIRS:
        if not proj.exists():
            continue
        out = _run(["find", str(proj), "-type", "f",
                     "-mmin", f"-{CHECKPOINT_WINDOW_HOURS * 60}",
                     "-not", "-path", "*/.git/*",
                     "-not", "-path", "*/__pycache__/*",
                     "-not", "-path", "*/.venv/*",
                     "-not", "-path", "*/node_modules/*",
                     "-not", "-path", "*/.pytest_cache/*",
                     "-not", "-name", "*.pyc",
                     "-not", "-name", "uv.lock",
                     "-not", "-path", "*/state.yaml",
                     "-not", "-path", "*/changelog*.yaml",
                     "-not", "-path", "*/handover.yaml",
                     "-not", "-path", "*/blueprint.yaml",
                     "-not", "-path", "*/.last-*",
                    ])
        for line in out.split("\n"):
            line = line.strip()
            if line:
                recent.append(line)
    return recent


def git_status():
    """Get git status summaries for project dirs."""
    result = {}
    for proj in PROJECT_DIRS:
        git_dir = proj / ".git"
        if not git_dir.exists():
            continue
        branch = _run(["git", "branch", "--show-current"], cwd=str(proj))
        stat = _run(["git", "diff", "--stat"], cwd=str(proj))
        untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(proj))
        result[str(proj)] = {
            "branch": branch or "unknown",
            "changes": stat if stat else "(clean)",
            "untracked": untracked.split("\n") if untracked else [],
        }
    return result


def checkpoint_hash(checkpoint):
    """Hash the mechanical data (excluding time) to detect changes."""
    if not checkpoint:
        return ""
    payload = {
        "recent_files": sorted(checkpoint.get("recent_files", [])),
        "git": checkpoint.get("git", {}),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def main():
    now = datetime.now(TZ)
    data = load_handover()

    # Preserve AI-written sections (decisions, known_issues, completed_log)
    # todo, in_progress, blocked, and done now live in tasks.yaml, not here
    ai_sections = {}
    for key in ("decisions", "known_issues", "completed_log"):
        if key in data:
            ai_sections[key] = data[key]

    # Build mechanical checkpoint
    recent_files = scan_recent_files()
    git_state = git_status()

    checkpoint = {
        "time": now.isoformat(),
        "recent_files": recent_files[:30],
        "git": git_state,
    }

    # Skip write if mechanical data unchanged
    old_hash = checkpoint_hash(data.get("last_checkpoint"))
    new_hash = checkpoint_hash(checkpoint)
    if old_hash and old_hash == new_hash:
        return 0

    data = {
        "last_checkpoint": checkpoint,
        "decisions": ai_sections.get("decisions", []),
        "known_issues": ai_sections.get("known_issues", []),
        "completed_log": ai_sections.get("completed_log", data.get("completed_log", [])),
    }

    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        save_handover(data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
