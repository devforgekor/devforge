#!/usr/bin/env python3
"""gen_motd_task_unified.py — Combine header + brief tasks for quick SSH overview.
Dependencies: gen_motd_system.py + gen_tasks.py (pre-executed)
Output: /tmp/devforge-motd-task.txt
"""
import subprocess
import sys
from pathlib import Path

import yaml

SERVER = Path("/opt/projects/server")
STATE_FILE = SERVER / "state.yaml"
HANDOVER_FILE = SERVER / "handover.yaml"
CLI = str(SERVER / "scripts/cli.py")

HEADER_FILE = Path("/tmp/devforge-header.txt")
TASKS_FILE_TMP = Path("/tmp/devforge-tasks.txt")
CACHE_FILE = Path("/tmp/devforge-motd-task.txt")

# ANSI colors
RED = "\033[0;31m"
NC = "\033[0m"
BOLD = "\033[1m"
YELLOW = "\033[0;33m"


def load_yaml(path: Path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def build_brief_tasks():
    """Extract brief task summary from tasks.yaml."""
    tasks = load_yaml(SERVER / "docs/tasks.yaml")
    
    in_progress = tasks.get("in_progress")
    todo_list = tasks.get("todo", [])
    blocked_list = tasks.get("blocked", [])
    
    lines = []
    
    # Current task
    if in_progress:
        title = in_progress if isinstance(in_progress, str) else in_progress.get("title", "")
        lines.append(f"  {YELLOW}Current:{NC} {title[:70]}")
    
    # Next TODO item
    if todo_list:
        title = todo_list[0] if isinstance(todo_list[0], str) else todo_list[0].get("title", "")
        lines.append(f"  {BOLD}Next:{NC} {title[:70]}")
    
    # Blocked items count
    if blocked_list:
        lines.append(f"  {RED}Blocked:{NC} {len(blocked_list)} item(s)")
    
    return "\n".join(lines) if lines else ""


def build_warnings():
    """Build brief warnings section from state.yaml."""
    state = load_yaml(STATE_FILE)
    metrics = state.get("metrics", {})
    
    warnings = []
    
    # CPU warning
    sys_m = metrics.get("system", {})
    cpu_load = sys_m.get("cpu_load", [0, 0, 0])
    cpu_15m = cpu_load[2]
    if cpu_15m >= 4:
        warnings.append(f"CPU 부하: 15분 평균 {cpu_15m:.1f}")
    
    # Memory warning
    mem = sys_m.get("memory", {})
    mem_pct = mem.get("percent", 0)
    if mem_pct >= 80:
        mem_used = mem.get("used", "?Gi")
        mem_total = mem.get("total", "?Gi")
        warnings.append(f"메모리: {mem_used}/{mem_total} ({mem_pct}%)")
    
    # Container unhealthy
    containers = state.get("structural", {}).get("containers", [])
    unhealthy = [c["name"] for c in containers if "unhealthy" in c.get("status", "").lower()]
    if unhealthy:
        warnings.append(f"컨테이너: {', '.join(unhealthy)}")
    
    # Service failed
    services = state.get("structural", {}).get("services", [])
    failed = [s["name"] for s in services if s.get("status") not in ("active", "inactive")]
    if failed:
        warnings.append(f"서비스: {', '.join(failed)}")
    
    if not warnings:
        return ""
    
    lines = [f"\n  {BOLD}[주의]{NC}"]
    for w in warnings:
        lines.append(f"    {w}")
    return "\n".join(lines)


def main():
    # Read pre-generated files
    header_line = HEADER_FILE.read_text().strip() if HEADER_FILE.exists() else ""
    
    # Build output
    output = []
    if header_line:
        output.append(header_line)
    
    brief_tasks = build_brief_tasks()
    if brief_tasks:
        output.append(f"\n{brief_tasks}")
    
    warnings = build_warnings()
    if warnings:
        output.append(warnings)
    
    text = "\n".join(output)
    
    # Write or print
    if "--stdout" in sys.argv:
        print(text)
    else:
        CACHE_FILE.write_text(text + "\n")
        print(f"✓ {CACHE_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
