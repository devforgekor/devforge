#!/usr/bin/env python3
# Status: experimental
# Path: hooks:PostToolUse in settings.json — auto-log tool calls to observations table
"""PostToolUse hook — auto-log tool calls to DB for session persistence."""

import datetime
import json
import os
import sys
import traceback

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_HOOKS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from lib.observation import observe

_ERROR_LOG = "/tmp/devforge-hook-errors.log"

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
    for pat in [
        "sk-",
        "ghp_",
        "gho_",
        "ghu_",
        "ghs_",
        "ghr_",
        "xoxb-",
        "xoxp-",
        "xoxa-",
        "xoxs-",
        "AIzaSy",
        "hf_",
        "BSA",
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


def _write_error_log(msg: str) -> None:
    """Append timestamped error to local log file for debugging."""
    try:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        with open(_ERROR_LOG, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _handle_bash(tool_input: dict, tool_output: dict) -> None:
    cmd = tool_input.get("command", "")
    if not cmd:
        return
    if not _is_test_command(cmd):
        return

    exit_code = tool_output.get("exitCode", -1)
    category = "test_result"
    detail = (
        "passed" if exit_code == 0 else f"exit={exit_code}" if exit_code >= 0 else "interrupted"
    )
    observation = f"test: {_redact(cmd, 120)} [{detail}]"
    context = {
        "tool": "Bash",
        "command": _redact(cmd, 300),
        "exit_code": exit_code,
    }
    tags = {"domain": ["test"]}

    observe(observation, category=category, source="hook:PostToolUse", context=context, tags=tags)


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
    }
    observation_text = f"edit: {file_path}"
    tags = {"domain": ["edit"]}

    observe(
        observation_text, category="edit", source="hook:PostToolUse", context=context, tags=tags
    )


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
    }
    observation_text = f"write: {file_path}"
    tags = {"domain": ["edit"]}

    observe(
        observation_text, category="edit", source="hook:PostToolUse", context=context, tags=tags
    )


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
    }
    observation_text = f"error: {tool_name} — {str(error)[:200]}"
    tags = {"domain": ["error"]}

    observe(
        observation_text, category="error", source="hook:PostToolUse", context=context, tags=tags
    )


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

    try:
        _handle_tool_error(tool_name, tool_input, tool_output)

        if tool_name == "Bash":
            _handle_bash(tool_input, tool_output)
        elif tool_name == "Edit":
            _handle_edit(tool_input, tool_output)
        elif tool_name == "Write":
            _handle_write(tool_input, tool_output)
    except Exception as e:
        tb = traceback.format_exc()
        _write_error_log(f"Unhandled in main: {e}\n{tb}")
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (f"[hook:auto_log] {tool_name} logging failed: {e}"),
            }
        }
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
