#!/usr/bin/env python3
# Status: production
# Path: none — manual (agent-invoked at session end); handover-gen.timer planned but absent
"""update_handover.py — Quality-scored session context capture.

Manual: invoked by the agent at session end. The SessionEnd hook runs
slack_notify.py and a handover-gen.timer does not exist (doc-vs-reality gap;
see docs/plans/agent-handover-db-design.md 후속).
Categorizes recent files by priority (source > config/docs > generated)
so downstream LLMs can distinguish real work from auto-generated noise.

Phase 2 Tier 1: quality-score-based prioritization of high-fidelity turns.
"""

import fcntl
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from lib.handover_db import db_write_checkpoint

SERVER_DIR = Path("/opt/projects/server")
HANDOVER_FILE = SERVER_DIR / "handover.yaml"
LOCK_FILE = SERVER_DIR / ".handover.lock"
PROJECT_DIRS = [
    Path("/opt/projects/server"),
]
TZ = timezone(timedelta(hours=9))
CHECKPOINT_WINDOW_HOURS = 1


# Priority order: source > config > docs > data > generated
# "generated" files are pure noise for LLM context — kept separately

CATEGORY_RULES: List[tuple] = [
    ("generated", [  # auto-generated — no LLM value
        "state.yaml", "changelog.yaml", ".last-structural-hash",
        "collect_checkpoint.json", "data/collect_status.yaml",
        "data/link_review.yaml", "data/nightly_status.yaml",
    ]),
    ("data", [  # machine-maintained data — low signal
        "link_turns.log",
    ]),
    ("config", [  # configuration — medium signal
        "CLAUDE.yaml", "blueprint.yaml", "code_mod_test_tasks.yaml",
        "*.timer", "*.service", ".service",
    ]),
    ("docs", [  # documentation — medium-high signal
        "docs/*.md", "docs/*.yaml",
    ]),
    ("source", [  # actual code — highest signal
        "*.py", "*.sh",
    ]),
]


def _run(cmd, timeout=15, cwd=str(SERVER_DIR)):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.stdout.strip()
    except Exception:
        return ""


def _relpath(abspath: str, proj: Path) -> str:
    """Relative path from project root for readability."""
    try:
        return str(Path(abspath).relative_to(proj))
    except ValueError:
        return abspath


def _classify(relpath: str) -> str:
    """Classify a file path into one of the 5 categories."""
    filename = Path(relpath).name
    for category, patterns in CATEGORY_RULES:
        for pat in patterns:
            # exact filename match
            if pat == filename:
                return category
            # exact relpath match
            if pat == relpath:
                return category
            # wildcard suffix match (e.g. "*.py", "docs/*.md")
            if pat.startswith("*.") and filename.endswith(pat[1:]):
                return category
            if pat.endswith("/*.md"):
                prefix = pat[:-5]
                if relpath.startswith(prefix) and relpath.endswith(".md"):
                    return category
            if pat.endswith("/*.yaml"):
                prefix = pat[:-6]
                if relpath.startswith(prefix) and relpath.endswith(".yaml"):
                    return category
    return "other"


def load_handover() -> dict:
    if HANDOVER_FILE.exists():
        with open(HANDOVER_FILE) as f:
            return yaml.safe_load(f) or {}
    return {}


def save_handover(data: dict):
    HANDOVER_FILE.write_text(yaml.dump(
        data, default_flow_style=False, allow_unicode=True,
        sort_keys=False, width=120))


def load_tasks() -> Optional[dict]:
    """Query tasks DB for in_progress context."""
    from lib.db import psql_json
    rows = psql_json("SELECT title, description FROM tasks WHERE status = 'in_progress' LIMIT 1")
    if rows:
        return {"in_progress": rows[0].get("title", "")}
    return None


def scan_recent_files() -> Dict[str, List[str]]:
    """Scan recent files, categorize by quality tier.

    Returns dict keyed by category, values sorted by path.
    'generated' files are filtered into their own bucket so
    downstream LLMs see source/config/docs first.
    """
    buckets: Dict[str, List[str]] = {
        "source": [], "config": [], "docs": [], "data": [], "generated": [], "other": []
    }

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
                     "-not", "-path", "*/handover.yaml",
                    ])
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            rel = _relpath(line, proj)
            cat = _classify(rel)
            buckets[cat].append(f"{proj.name}/{rel}")

    # sort within each category
    for cat in buckets:
        buckets[cat].sort()

    return buckets


def git_summary() -> dict:
    """Lightweight git status — branch + change count per project."""
    result = {}
    for proj in PROJECT_DIRS:
        git_dir = proj / ".git"
        if not git_dir.exists():
            continue
        branch = _run(["git", "branch", "--show-current"], cwd=str(proj))
        shortstat = _run(["git", "diff", "--shortstat"], cwd=str(proj))
        staged = _run(["git", "diff", "--cached", "--shortstat"], cwd=str(proj))
        untracked_count = _run(
            ["git", "ls-files", "--others", "--exclude-standard"], cwd=str(proj))
        untracked = len(untracked_count.split("\n")) if untracked_count else 0
        result[proj.name] = {
            "branch": branch or "unknown",
            "unstaged": shortstat or "(clean)",
            "staged": staged or "(none)",
            "untracked_files": untracked,
        }
    return result


def task_context() -> Optional[str]:
    """Extract in_progress task title for checkpoint context."""
    tasks = load_tasks()
    if not tasks:
        return None
    in_progress = tasks.get("in_progress")
    if isinstance(in_progress, str):
        # first 120 chars — enough to identify the task
        return in_progress[:120]
    return None


def checkpoint_hash(checkpoint: dict) -> str:
    """Hash the mechanical data (excluding time) to detect changes."""
    if not checkpoint:
        return ""
    payload = {
        "recent_files": checkpoint.get("recent_files", {}),
        "git": checkpoint.get("git", {}),
        "task": checkpoint.get("task", ""),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def main():
    now = datetime.now(TZ)
    data = load_handover()

    # Preserve AI-written sections
    ai_sections = {}
    for key in ("decisions", "known_issues", "completed_log"):
        if key in data:
            ai_sections[key] = data[key]

    recent = scan_recent_files()
    git_state = git_summary()
    task_ctx = task_context()

    # Build quality-scored summary
    total_files = sum(len(v) for v in recent.values())
    summary_parts = []
    priority_order = ["source", "config", "docs", "data", "generated"]
    for cat in priority_order:
        count = len(recent[cat])
        if count:
            summary_parts.append(f"{count} {cat}")

    checkpoint = {
        "time": now.isoformat(),
        "summary": ", ".join(summary_parts) if summary_parts else "no files",
        "total_files": total_files,
        "recent_files": {
            "source": recent["source"][:20],
            "config": recent["config"][:10],
            "docs": recent["docs"][:10],
            "data": recent["data"][:10],
            "generated": recent["generated"][:5],
        },
        "git": git_state,
        "task": task_ctx,
    }

    old_hash = checkpoint_hash(data.get("last_checkpoint"))
    new_hash = checkpoint_hash(checkpoint)
    if old_hash and old_hash == new_hash:
        # File content unchanged — skip YAML save but still persist decisions from YAML to DB
        if ai_sections.get("decisions") or ai_sections.get("known_issues"):
            db_write_checkpoint(checkpoint, ai_sections, skip_checkpoint=True)
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

    # Write checkpoint to DB (primary store)
    db_write_checkpoint(checkpoint, data)

    return 0


if __name__ == "__main__":
    sys.exit(main())
