#!/usr/bin/env python3
# Status: experimental
# Path: PreToolUse hook -- settings.json (Bash matcher)
"""PreToolUse hook: log ALL Bash tool call attempts as observations.

PostToolUse (auto_log.py) logs executed calls. PreToolUse logs the attempt
before permission check. Bash calls logged by PreToolUse but NOT by
PostToolUse = denied/blocked by permission system.

Usage:
  - This hook is triggered BEFORE Bash execution (and BEFORE permission check)
  - Reads stdin (same format as PostToolUse hook)
  - Writes observation: category='tool_attempt', source='pretool:bash'
"""

import json
import subprocess
import sys

# Read tool call from stdin (same format as PostToolUse hooks)
raw = sys.stdin.read()
if not raw:
    sys.exit(0)
try:
    event = json.loads(raw)
except (json.JSONDecodeError, EOFError):
    sys.exit(0)

tool_name = event.get("tool_name", "")
tool_input = event.get("tool_input", {})

# Only log Bash attempts
if tool_name != "Bash":
    sys.exit(0)

# Extract first command token (safe, truncated)
cmd = (tool_input.get("command") or "")[:200]
cmd_start = cmd.strip().split()[0] if cmd.strip() else "unknown"

# Tags: command is a plain string for ->'command' matching in auto_log.py
tags = {
    "tool": "Bash",
    "command": cmd_start,
    "status": "attempted",
}

context = {
    "tool": "Bash",
    "command": cmd_start,
    "input_preview": cmd[:120],
}

sql = (
    "INSERT INTO observations (observation, category, source, context, tags) VALUES ("
    f"'[tool_attempt] Bash: {cmd_start}', "
    f"'tool_attempt', 'pretool:bash', "
    f"$JSON${json.dumps(context, ensure_ascii=False)}$JSON$::jsonb, "
    f"$JSON${json.dumps(tags, ensure_ascii=False)}$JSON$::jsonb"
    f")"
)

try:
    r = subprocess.run(
        [
            "podman",
            "exec",
            "-i",
            "postgres",
            "psql",
            "-U",
            "devforge",
            "-d",
            "devforge_app",
            "-c",
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if r.returncode != 0:
        pass  # silent — errors logged to DB by auto_log.py PostToolUse
except Exception:
    pass  # silent — hook errors are noise
