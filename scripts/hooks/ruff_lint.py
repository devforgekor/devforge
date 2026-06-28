#!/usr/bin/env python3
# Status: experimental
# Path: PostToolUse hook in settings.json — auto-fix + lint feedback via additionalContext
"""PostToolUse hook — auto-fix Python files with ruff, surface remaining violations."""

import json
import subprocess
import sys

FILE_EXT = (".py", ".pyi")
MAX_LINES = 15


def _should_process(file_path: str) -> bool:
    return (
        isinstance(file_path, str)
        and file_path.startswith("/opt/projects/server")
        and file_path.endswith(FILE_EXT)
    )


def _ruff(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return str(e)


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, EOFError):
        return

    tool_name = event.get("tool_name", "")
    file_path = event.get("tool_input", {}).get("file_path", "")

    if tool_name not in ("Write", "Edit") or not _should_process(file_path):
        return

    # Phase 1: auto-fix silently
    _ruff(["ruff", "check", "--fix", "--quiet", file_path])
    _ruff(["ruff", "format", "--quiet", file_path])

    # Phase 2: re-check for unfixable violations
    output = _ruff(["ruff", "check", file_path]).strip()
    if not output:
        return

    lines = [ln for ln in output.split("\n") if ln.strip()][:MAX_LINES]
    if not lines:
        return

    out = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"[ruff] {file_path}:\n" + "\n".join(lines),
        }
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
