#!/usr/bin/env python3
# Status: experimental
# Path: tests/fitness/
"""Fitness: every declared wiring points at something that exists.

Catches the 'silent no-op' class — a timer/service/hook that references a
script (or peer unit) that is missing, so the trigger silently does nothing.

Scope (repo mirrors, CI-portable): systemd/user/*.service, systemd/user/*.timer,
containers/systemd/*.container. ~/.claude/settings.json hooks are checked only
when the file exists (local host).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_PATH_RE = re.compile(r"/opt/projects/server/[A-Za-z0-9_./-]+")
EXEC_RE = re.compile(r"^Exec(?:Start|StartPre|StartPost)?=(.*)$", re.MULTILINE)
UNIT_RE = re.compile(r"^Unit=(\S+)$", re.MULTILINE)


def _server_paths(text: str) -> set[str]:
    return set(SERVER_PATH_RE.findall(text))


def test_service_exec_paths_exist() -> None:
    missing: list[str] = []
    for unit in sorted((ROOT / "systemd/user").glob("*.service")) + sorted(
        (ROOT / "containers/systemd").glob("*.container")
    ):
        for path in _server_paths(unit.read_text(encoding="utf-8")):
            if not Path(path).exists():
                missing.append(f"{unit.name}: {path}")
    assert not missing, f"ExecStart references missing files: {missing}"


def test_timer_services_exist() -> None:
    missing: list[str] = []
    for timer in sorted((ROOT / "systemd/user").glob("*.timer")):
        text = timer.read_text(encoding="utf-8")
        m = UNIT_RE.search(text)
        target = m.group(1) if m else f"{timer.stem}.service"
        if not (ROOT / "systemd/user" / target).exists():
            missing.append(f"{timer.name} -> {target}")
    assert not missing, f"timers reference missing services: {missing}"


def test_hook_commands_reference_existing_scripts() -> None:
    settings = Path.home() / ".claude/settings.json"
    if not settings.exists():
        return  # CI / non-host: skip
    data = json.loads(settings.read_text(encoding="utf-8"))
    missing: list[str] = []
    for _event, groups in (data.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                for path in _server_paths(hook.get("command", "")):
                    if not Path(path).exists():
                        missing.append(f"hook: {path}")
    assert not missing, f"hooks reference missing scripts: {missing}"
