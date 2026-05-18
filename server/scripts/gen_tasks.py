#!/usr/bin/env python3
"""gen_tasks.py — Generate detailed DevForge tasks by AI agent.
Output: /tmp/devforge-tasks.txt
Data: tasks.yaml + handover.yaml + DB (yesterday's activity)
"""
import subprocess
import sys
from pathlib import Path

import yaml

SERVER = Path("/opt/projects/server")
TASKS_FILE = SERVER / "docs/tasks.yaml"
HANDOVER_FILE = SERVER / "handover.yaml"
CACHE_FILE = Path("/tmp/devforge-tasks.txt")

# ANSI colors
BOLD = "\033[1m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
GREEN = "\033[0;32m"
NC = "\033[0m"

AGENT_ORDER = ("claude-code", "copilot", "gemini", "qwen")
AGENT_DISPLAY = {
    "claude-code": "Claude",
    "copilot": "Copilot",
    "gemini": "Gemini",
    "qwen": "Qwen",
}


def load_yaml(path: Path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def psql(query: str, timeout=5):
    """Execute PostgreSQL query via podman."""
    try:
        r = subprocess.run(
            ["podman", "exec", "postgres", "psql", "-U", "devforge",
             "-d", "devforge_app", "--no-align", "--tuples-only", "-c", query],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def get_yesterday_stats(agent):
    """Get yesterday's activity for an agent."""
    out = psql(
        f"SELECT COUNT(*) as turns, COUNT(d.turn_id) as decisions "
        f"FROM turns t LEFT JOIN obs_dec d ON d.turn_id = t.id "
        f"WHERE t.agent = '{agent}' "
        f"AND t.created_at >= CURRENT_DATE - 1 "
        f"AND t.created_at < CURRENT_DATE"
    )
    try:
        if out:
            parts = out.split("|")
            turns = int(parts[0].strip())
            decisions = int(parts[1].strip())
            return turns, decisions
    except Exception:
        pass
    return 0, 0


def main():
    tasks = load_yaml(TASKS_FILE)
    handover = load_yaml(HANDOVER_FILE)
    
    lines = []
    
    # ─── OVERALL SUMMARY ────────────────────────────────────────
    lines.append(f"{BOLD}[TASKS SUMMARY]{NC}")
    
    todo_list = tasks.get("todo", [])
    done_list = tasks.get("done", [])
    blocked_list = tasks.get("blocked", [])
    in_progress = tasks.get("in_progress")
    
    total_tasks = len(todo_list) + len(done_list) + len(blocked_list) + (1 if in_progress else 0)
    
    lines.append(f"  Total: {len(done_list)} done | {len(todo_list)} todo | {len(blocked_list)} blocked | {1 if in_progress else 0} in-progress")
    
    # Current task
    if in_progress:
        title = in_progress if isinstance(in_progress, str) else in_progress.get("title", "")
        lines.append(f"  {YELLOW}Current:{NC} {title[:80]}")
    
    # ─── AGENT-SPECIFIC SECTIONS ────────────────────────────────
    for agent in AGENT_ORDER:
        agent_display = AGENT_DISPLAY[agent]
        turns, decisions = get_yesterday_stats(agent)
        
        lines.append(f"\n{BOLD}[{agent_display.upper()}]{NC}")
        
        # Yesterday activity
        if turns > 0 or decisions > 0:
            lines.append(f"  Yesterday: {turns} turns, {decisions} decisions")
        else:
            lines.append(f"  Yesterday: (no activity)")
        
        # Find tasks/decisions for this agent
        agent_tasks = []
        for item in todo_list + done_list + blocked_list:
            title = item if isinstance(item, str) else item.get("title", "")
            if agent.lower() in title.lower():
                agent_tasks.append(title)
        
        if agent_tasks:
            lines.append(f"  Tasks ({len(agent_tasks)}):")
            for task in agent_tasks[:3]:
                lines.append(f"    • {task[:75]}")
            if len(agent_tasks) > 3:
                lines.append(f"    ... +{len(agent_tasks) - 3} more")
        
        # Agent-specific decisions from handover
        decisions_dict = handover.get("decisions", {})
        agent_decisions = [
            (k, v) for k, v in decisions_dict.items() 
            if agent.lower() in k.lower()
        ]
        
        if agent_decisions:
            lines.append(f"  Decisions ({len(agent_decisions)}):")
            for key, value in agent_decisions[:2]:
                desc = value if isinstance(value, str) else value.get("description", str(value))[:60]
                lines.append(f"    • {key}: {desc}")
            if len(agent_decisions) > 2:
                lines.append(f"    ... +{len(agent_decisions) - 2} more")
    
    # ─── BLOCKED ITEMS ──────────────────────────────────────────
    if blocked_list:
        lines.append(f"\n{BOLD}[BLOCKED]{NC} ({len(blocked_list)})")
        for item in blocked_list[:5]:
            title = item if isinstance(item, str) else item.get("title", "")
            lines.append(f"  x {title[:75]}")
        if len(blocked_list) > 5:
            lines.append(f"  ... +{len(blocked_list) - 5} more")
    
    # ─── GLOBAL DECISIONS ───────────────────────────────────────
    decisions = handover.get("decisions", {})
    if decisions:
        lines.append(f"\n{BOLD}[GLOBAL DECISIONS]{NC} ({len(decisions)})")
        for key, value in list(decisions.items())[:5]:
            desc = value if isinstance(value, str) else value.get("description", str(value))[:60]
            lines.append(f"  • {key}: {desc}")
        if len(decisions) > 5:
            lines.append(f"  ... +{len(decisions) - 5} more")
    
    # ─── KNOWN ISSUES ───────────────────────────────────────────
    known_issues = handover.get("known_issues", {})
    if known_issues:
        lines.append(f"\n{BOLD}[KNOWN ISSUES]{NC} ({len(known_issues)})")
        for key, value in list(known_issues.items())[:3]:
            desc = value if isinstance(value, str) else value.get("description", str(value))[:60]
            lines.append(f"  ⚠ {key}: {desc}")
        if len(known_issues) > 3:
            lines.append(f"  ... +{len(known_issues) - 3} more")
    
    text = "\n".join(lines)
    
    if "--stdout" in sys.argv:
        print(text)
    else:
        CACHE_FILE.write_text(text + "\n")
        print(f"✓ {CACHE_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
