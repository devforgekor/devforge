#!/usr/bin/env python3
# Status: experimental
# Path: hooks:PostToolUse in settings.json — auto-log tool calls to observations table
"""PostToolUse hook — silently log tool calls to DB for session persistence."""
import json
import os
import sys
import traceback

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_HOOKS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.db import psql

_OBSERVATIONS_INSERT = (
    "INSERT INTO observations (observation, category, source, context) VALUES "
)

TEST_PATTERNS = [
    "pytest",
    "test_predicate_extract",
    "test_setup(",
    "test_heartbeat(",
    "test_complete()",
    "ensure_model(",
    "pytest",
    "unittest",
]


def _redact(value: str, max_len: int = 200) -> str:
    """Truncate and redact secrets from a string value."""
    if not isinstance(value, str):
        return str(value)[:max_len]
    s = value.strip()
    # redact obvious secrets
    for pat in [
        "sk-", "ghp_", "gho_", "ghu_", "ghs_", "ghr_",
        "xoxb-", "xoxp-", "xoxa-", "xoxs-",
        "AIzaSy", "hf_", "BSA",
    ]:
        idx = s.find(pat)
        if idx >= 0:
            s = s[:idx] + f"[REDACTED:{pat[:3]}]"
            break
    if len(s) > max_len:
        s = s[:max_len] + "..."
    return s


def _is_test_command(cmd: str) -> bool:
    cmd_lower = cmd.lower()
    for pat in TEST_PATTERNS:
        if pat.lower() in cmd_lower:
            return True
    return False


def _is_config_or_source_edit(path: str) -> bool:
    important_prefixes = [
        "/opt/projects/server/scripts/pipelines/",
        "/opt/projects/server/scripts/hooks/",
        "/opt/projects/server/scripts/tests/",
        "/opt/projects/server/scripts/lib/",
        "/opt/projects/server/scripts/proxies/",
        "/home/opc/.claude/settings.json",
        "/home/opc/.claude/mcp.json",
        "/home/opc/.claude/CLAUDE.md",
    ]
    for prefix in important_prefixes:
        if path.startswith(prefix):
            return True
    return False


def _log_observation(observation: str, category: str, context: dict) -> None:
    """Insert a row into observations table. Silently ignores DB errors."""
    try:
        ctx_json = json.dumps(context, ensure_ascii=False, default=str)
        obs_escaped = observation.replace("'", "''")
        sql = (
            f"{_OBSERVATIONS_INSERT}("
            f"'{obs_escaped}', '{category}', 'hook:PostToolUse', '{ctx_json}'::jsonb"
            f")"
        )
        psql(sql)
    except Exception:
        # silent failure — hook must never break the tool call
        pass


def _handle_bash(tool_input: dict, tool_output: dict) -> None:
    cmd = tool_input.get("command", "")
    if not cmd:
        return

    exit_code = tool_output.get("exitCode", -1)
    is_test = _is_test_command(cmd)

    if not is_test:
        return

    context = {
        "tool": "Bash",
        "command": _redact(cmd, 300),
        "exit_code": exit_code,
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
    }

    if is_test:
        category = "test_result"
        detail = "passed" if exit_code == 0 else f"exit={exit_code}" if exit_code >= 0 else "interrupted"
        observation = f"test: {_redact(cmd, 120)} [{detail}]"
    else:
        return

    _log_observation(observation, category, context)


def _handle_edit(tool_input: dict, tool_output: dict) -> None:
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    if not _is_config_or_source_edit(file_path):
        return

    new_str = tool_input.get("new_string", "")
    old_str = tool_input.get("old_string", "")

    context = {
        "tool": "Edit",
        "file_path": file_path,
        "old_len": len(old_str),
        "new_len": len(new_str),
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
    }
    observation = f"edit: {file_path}"
    _log_observation(observation, "edit", context)


def _handle_write(tool_input: dict, tool_output: dict) -> None:
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    if not _is_config_or_source_edit(file_path):
        return

    content = tool_input.get("content", "")
    context = {
        "tool": "Write",
        "file_path": file_path,
        "content_len": len(content),
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
    }
    observation = f"write: {file_path}"
    _log_observation(observation, "edit", context)


def _handle_tool_error(tool_name: str, tool_input: dict, tool_output: dict) -> None:
    """Log tool errors if present."""
    if not isinstance(tool_output, dict):
        return
    error = tool_output.get("error")
    if not error:
        return
    context = {
        "tool": tool_name,
        "error": str(error)[:500],
        "session_id": os.environ.get("CLAUDE_SESSION_ID", ""),
    }
    observation = f"error: {tool_name} — {str(error)[:200]}"
    _log_observation(observation, "error", context)


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw:
            return
        event = json.loads(raw)
    except (json.JSONDecodeError, EOFError):
        return

    tool_name = event.get("tool_name", "")
    tool_input = event.get("tool_input", {})
    tool_output = event.get("tool_output", {})

    # Always log errors
    _handle_tool_error(tool_name, tool_input, tool_output)

    # Route by tool type
    if tool_name == "Bash":
        _handle_bash(tool_input, tool_output)
    elif tool_name == "Edit":
        _handle_edit(tool_input, tool_output)
    elif tool_name == "Write":
        _handle_write(tool_input, tool_output)


if __name__ == "__main__":
    main()
