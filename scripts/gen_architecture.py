#!/usr/bin/env python3
"""Generate infrastructure.md and software.yaml from CLAUDE.yaml.

Reads CLAUDE.yaml and writes:
  - /home/opc/infrastructure.md    (@include for all AI sessions)
  - /opt/projects/server/docs/architecture/software.yaml  (reference YAML)

Also validates code-structure.yaml matches actual files on disk.

Run via systemd timer: devforge-gen-architecture.timer (daily, KST 09:00).
"""

import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

CLAUDE_YAML = Path("/opt/projects/server/CLAUDE.yaml")
CODE_STRUCTURE_YAML = Path("/opt/projects/server/docs/architecture/code-structure.yaml")
SCRIPTS_DIR = Path("/opt/projects/server/scripts")
INFRA_OUTPUT = Path("/home/opc/infrastructure.md")
SOFTWARE_OUTPUT = Path("/opt/projects/server/docs/architecture/software.yaml")
KST = timezone(timedelta(hours=9))


def _q(v):
    return str(v) if v is not None else "—"


# ── infrastructure.md ────────────────────────────────────────────────

def _build_overview(data: dict) -> str:
    ov = data.get("overview", {})
    lines = [
        "## Overview",
        f"- Host: {_q(ov.get('hostname')).upper()} ({_q(ov.get('cpu'))}, {_q(ov.get('memory'))})",
        f"- OS: {_q(ov.get('os'))}",
    ]
    containers = data.get("containers", {})
    runtime = containers.get("runtime", "")
    lines.append(f"- Runtime: {_q(runtime)}")
    lines.append("- Database: PostgreSQL 16 (pg_trgm, JSONB)")
    lines.append("- Proxy: Caddy (host network, auto-HTTPS)")
    lines.append("- Claude Code: DeepSeek v4-Flash (via Anthropic API compat at api.deepseek.com)")
    lines.append("- Aider: DeepSeek v4-flash (via OpenAI API compat at api.deepseek.com)")
    lines.append("- Resource usage: see `state.yaml#metrics` for live numbers")
    return "\n".join(lines)


def _build_infra_diagram(data: dict) -> str:
    return """## Infrastructure
```
data-pod (2GB)
└─ postgres :5432

Caddy (host network)
├─ /devforge/* → turn_watcher.py (via DB)
└─ netdata → localhost:19999
```"""


def _build_health_checks(data: dict) -> str:
    return """## Health Checks
- postgres (host): `pg_isready -h localhost -p 5432` (localhost only works if port is published; postgres binds to devforge-net, not host)
- postgres (always works): `podman exec postgres psql -U devforge -d devforge_app -c "SELECT 1"`
- **Rule**: Always use `podman exec postgres psql` for DB queries. `psql -h localhost` will fail because PostgreSQL runs in a podman container without host port publishing."""


def _build_network(data: dict) -> str:
    lines = [
        "## Network",
        "- Bridge: devforge-net (10.89.0.0/24)",
        "- SSH: 22/tcp (key-only)",
        "- Internal APIs: 127.0.0.1 only",
    ]
    return "\n".join(lines)


def _build_storage(data: dict) -> str:
    storage = data.get("storage", {})
    lines = ["## Storage"]
    for disk_key in ["boot_disk", "data_disk"]:
        disk = storage.get(disk_key, {})
        if not disk:
            continue
        for lv in disk.get("lvs", []):
            mount = lv.get("mount", "unmounted")
            if mount == "unmounted" or not lv.get("size"):
                continue
            if mount == "/opt/workspace":
                continue
            label = lv.get("lv", "").replace("lv_", "")
            lines.append(f"- `{_q(mount)}` ({_q(lv['size'])}) — {_q(label)}")
    lines.append("- `/opt/workspace` (6GB) — out of scope (consolidated into `/opt/projects/server/`)")
    return "\n".join(lines)


def _build_services(data: dict) -> str:
    services = data.get("services", [])
    lines = [
        "## Key Services",
        "| Service | Type | Status |",
        "|---------|------|--------|",
    ]
    for s in services:
        if isinstance(s, dict):
            name = s.get("name", "?")
            stype = s.get("type", "systemd user")
            status = s.get("status", "?")
            lines.append(f"| {name} | {stype} | {status} |")
    return "\n".join(lines)


def _build_logs(data: dict) -> str:
    return """## Logs
- Services: `journalctl --user -u <service-name>`
- Caddy: `journalctl -u caddy`"""


def _build_entry_points(data: dict) -> str:
    lines = [
        "## Entry Points",
        "- `/opt/projects/server/CLAUDE.yaml` — server harness, auto-generated state",
        "- `/opt/projects/server/docs/` — design, phases, tasks",
        "- `devforge_app.worklog_entries` — work log (DB)",
    ]
    return "\n".join(lines)


def _gen_infrastructure(data: dict) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    sections = [
        "# DevForge — Server Identity",
        f"<!-- auto-generated from CLAUDE.yaml at {now} -->",
        "",
        _build_overview(data),
        "",
        _build_infra_diagram(data),
        "",
        _build_health_checks(data),
        "",
        _build_network(data),
        "",
        _build_storage(data),
        "",
        _build_services(data),
        "",
        _build_logs(data),
        "",
        _build_entry_points(data),
        "",
    ]
    return "\n".join(sections)


# ── software.yaml ──────────────────────────────────────────────────

def _build_software(data: dict) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    soft = data.get("software", {})

    models = soft.get("models") or []
    modes = soft.get("modes") or {}
    pipelines = soft.get("pipelines") or {}

    doc = {
        "metadata": {
            "updated": now,
            "source": "CLAUDE.yaml.software",
        },
        "models": models if models else [{"note": "Populate CLAUDE.yaml `software.models`"}],
        "modes": modes if modes else {"placeholder": {"note": "Populate CLAUDE.yaml `software.modes`"}},
        "pipelines": pipelines if pipelines else {"placeholder": "Populate CLAUDE.yaml `software.pipelines`"},
        "note": (
            "Add model/mode/pipeline data to CLAUDE.yaml `software:` key. "
            "Until then, see archived agent-architecture.yaml and "
            "server-specs-and-llm-architecture.md in `_archive/`."
        ),
    }

    header = (
        "# DevForge — Software Architecture\n"
        f"# auto-generated from CLAUDE.yaml at {now}\n"
        "---\n"
    )
    return header + yaml.dump(doc, default_flow_style=False, sort_keys=False)


# ── code-structure.yaml tree validation ────────────────────────────

def _validate_code_tree() -> list[str]:
    """Check scripts/ files against code-structure.yaml entries.

    Returns warnings for files on disk not listed in code-structure.yaml.
    """
    with open(CODE_STRUCTURE_YAML) as f:
        struct = yaml.safe_load(f)

    modules = struct.get("modules", [])
    listed = set()
    ignore_patterns = {"__pycache__", ".pyc", ".git"}

    for mod in modules:
        for fname, _ in mod.get("files", {}).items():
            listed.add(fname)
        files_in_group = mod.get("files", {})
        for fname in files_in_group:
            listed.add(fname)

    # Collect actual .py and .sh files in scripts/ root
    on_disk = set()
    for p in sorted(SCRIPTS_DIR.iterdir()):
        if any(pat in p.name for pat in ignore_patterns):
            continue
        if p.suffix in (".py", ".sh") and p.is_file():
            on_disk.add(p.name)

    unlisted = on_disk - listed
    warnings = []
    if unlisted:
        warnings.append(f"[tree-warn] Files on disk NOT in code-structure.yaml: {sorted(unlisted)}")
    return warnings


# ── main ────────────────────────────────────────────────────────────

def _write_if_changed(path: Path, content: str, label: str) -> bool:
    if path.exists() and path.read_text() == content:
        print(f"[{label}] unchanged, skip")
        return False
    path.write_text(content)
    print(f"[{label}] written")
    return True


def main():
    with open(CLAUDE_YAML) as f:
        data = yaml.safe_load(f)

    _write_if_changed(INFRA_OUTPUT, _gen_infrastructure(data), "infrastructure.md")
    SOFTWARE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(SOFTWARE_OUTPUT, _build_software(data), "software.yaml")

    # Tree validation
    for warn in _validate_code_tree():
        print(warn)


if __name__ == "__main__":
    main()
