#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""Generate infrastructure.md, software.yaml, code-structure.yaml, and timer-registry.yaml
from live system data (collect_structural + systemd + file scan).

Calls collect_structural() directly (no CLAUDE.yaml relay) for live server state.
Reads CLAUDE.yaml only for static overview config.

Outputs (all AI agents read infrastructure.md via @include):
  - /home/opc/infrastructure.md       (@include — server identity + doc index)
  - docs/architecture/infrastructure.md  (same, published to docs dir)
  - docs/architecture/software.yaml      (live models, modes, pipelines)
  - docs/architecture/code-structure.yaml (file layout, hash-guarded 15m sync)
  - docs/specs/timer-registry.yaml       (live systemd timer schedule)
"""

import os
import sys

import yaml
from datetime import datetime, timezone, timedelta
from pathlib import Path

CLAUDE_YAML = Path("/opt/projects/server/CLAUDE.yaml")
CODE_STRUCTURE_YAML = Path("/opt/projects/server/docs/architecture/code-structure.yaml")
SCRIPTS_DIR = Path("/opt/projects/server/scripts")
INFRA_OUTPUT = Path("/home/opc/infrastructure.md")
ARCH_INFRA_OUTPUT = Path("/opt/projects/server/docs/architecture/infrastructure.md")
SOFTWARE_OUTPUT = Path("/opt/projects/server/docs/architecture/software.yaml")
TIMER_REGISTRY_OUTPUT = Path("/opt/projects/server/docs/specs/timer-registry.yaml")
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


def _build_storage(structural: dict) -> str:
    storage = structural.get("storage", [])
    lines = ["## Storage"]
    for s in storage:
        mount = s.get("mount") or "unmounted"
        if mount == "unmounted" or not s.get("size"):
            continue
        if mount == "/opt/workspace":
            continue
        label = s.get("lv", "").replace("lv_", "")
        lines.append(f"- `{_q(mount)}` ({_q(s['size'])}) — {_q(label)}")
    lines.append("- `/opt/workspace` (6GB) — out of scope (consolidated into `/opt/projects/server/`)")
    return "\n".join(lines)


def _build_services(structural: dict, claude: dict) -> str:
    live_status = {s["name"]: s["status"] for s in structural.get("services", [])
                   if isinstance(s, dict) and "name" in s}
    old = claude.get("services", [])
    lines = [
        "## Key Services",
        "| Service | Type | Status |",
        "|---------|------|--------|",
    ]
    for s in old:
        if isinstance(s, dict):
            name = s.get("name", "?")
            status = live_status.get(name, s.get("status", "?"))
            stype = s.get("type", "systemd user")
            lines.append(f"| {name} | {stype} | {status} |")
    return "\n".join(lines)


def _build_logs(data: dict) -> str:
    return """## Logs
- Services: `journalctl --user -u <service-name>`
- Caddy: `journalctl -u caddy`"""


def _build_entry_points(data: dict) -> str:
    lines = [
        "## Entry Points",
        "- `cli.py status --json` — live state (containers, models, timers, services, resources, tasks, alerts)",
        "- `/opt/projects/server/docs/` — design, phases, glossary, specs",
        "- `devforge_app.worklog_entries` — work log (DB)",
        "",
        "### Auto-Generated Docs (live data, no manual edit)",
        "- `docs/architecture/infrastructure.md` — server identity (this doc)",
        "- `docs/architecture/software.yaml` — models, modes, pipelines (live)",
        "- `docs/architecture/code-structure.yaml` — file layout SSOT (script scan)",
        "- `docs/specs/timer-registry.yaml` — systemd timer schedule (live)",
    ]
    return "\n".join(lines)


def _gen_infrastructure(structural: dict, claude: dict) -> str:
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    sections = [
        "# DevForge — Server Identity",
        f"<!-- auto-generated from collect_structural() + CLAUDE.yaml at {now} -->",
        "",
        _build_overview(claude),
        "",
        _build_infra_diagram(structural),
        "",
        _build_health_checks(structural),
        "",
        _build_network(structural),
        "",
        _build_storage(structural),
        "",
        _build_services(structural, claude),
        "",
        _build_logs(structural),
        "",
        _build_entry_points(structural),
        "",
    ]
    return "\n".join(sections)


# ── software.yaml ──────────────────────────────────────────────────

MODE_FILE = Path("/opt/ai_data/scripts/current-system-mode.env")


def _build_software(structural: dict, claude: dict) -> str:
    import re

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")

    # Models from live containers — query /v1/models for running model name
    from lib.infra.containers import query_llama_model

    models = []
    for c in structural.get("containers", []):
        if not ("llama" in c.get("image", "") or c.get("model")):
            continue
        ports = c.get("ports", "")
        port = None
        m = re.search(r"(\d+)(?:-\d+)?->\d+", ports)
        if m:
            port = int(m.group(1))
        model_name = c.get("model") or (query_llama_model(port) if port else "")
        models.append({
            "name": model_name or c.get("name", "?"),
            "container": c.get("name", "?"),
            "status": c.get("status", "?"),
            "ports": ports,
        })

    # Current mode from env file
    current_mode = "unknown"
    try:
        current_mode = MODE_FILE.read_text().strip().split("=", 1)[1]
    except Exception:
        pass
    modes = {
        "current": current_mode,
        "available": {
            "day": "Pod A(3B:8082) + Pod B(Codestral-22B:8080)",
            "night": "Nightly review pipeline (verify + debate)",
        },
    }

    # Pipelines from structural services
    pipelines = {}
    for s in structural.get("services", []):
        if isinstance(s, dict):
            name = s.get("name", "")
            if any(k in name for k in ("classify", "extract", "nightly", "daily", "15m", "backup")):
                pipelines[name] = s.get("status", "?")

    doc = {
        "metadata": {
            "updated": now,
            "source": "live (collect_structural + mode file)",
        },
        "models": models if models else [{"note": "No running LLM containers detected"}],
        "modes": modes,
        "pipelines": pipelines if pipelines else {"note": "No pipeline services detected"},
    }

    header = (
        "# DevForge — Software Architecture\n"
        f"# auto-generated from live data at {now}\n"
        "---\n"
    )
    return header + yaml.dump(doc, default_flow_style=False, sort_keys=False)


# ── code-structure.yaml auto-sync ──────────────────────────────

CODE_STRUCTURE_HEADER = """# ═══════════════════════════════════════════════════════════════════════════
# DevForge Code Structure — Single Source of Truth for File Layout
# ═══════════════════════════════════════════════════════════════════════════
# 🔴 CRITICAL: LLM MUST read this before creating, moving, or modifying any file.
# 🔴 If unsure where a change belongs, check here first. Ask user if still unclear.
# 🔴 Do NOT create new files under `scripts/` without user approval.
#
# Rule priority: code-structure.yaml > llm-common-rule.md > agent's judgment
# If this file says a module has `no_subprocess: true`, do NOT add subprocess calls there.
# ═══════════════════════════════════════════════════════════════════════════

"""


IGNORE_SUFFIXES = {".pyc"}
IGNORE_DIRS = {"_archive", "__pycache__", ".git"}
SCRIPTS_LIB = SCRIPTS_DIR / "lib"


def _discover_dirs() -> list[Path]:
    """Discover all subdirectories under scripts/ that contain .py or .sh files."""
    dirs = [SCRIPTS_DIR]
    # Explicitly add scripts/lib/
    dirs.append(SCRIPTS_LIB)
    # Recurse lib/ subdirectories
    for lib_sub in sorted(SCRIPTS_LIB.rglob("*")):
        if lib_sub.is_dir() and lib_sub.name not in IGNORE_DIRS and "_archive" not in lib_sub.parts:
            dirs.append(lib_sub)
    # Other root-level subdirectories (pipelines/, proxies/, state_collector/)
    for sub in sorted(SCRIPTS_DIR.iterdir()):
        if not sub.is_dir() or sub.name in IGNORE_DIRS or "_archive" in sub.parts:
            continue
        if sub == SCRIPTS_LIB:
            continue  # already handled above
        dirs.append(sub)
    return dirs


def _infer_group_purpose(path: Path) -> str:
    """Infer a human-readable purpose from directory path."""
    rel = path.relative_to(SCRIPTS_DIR)
    parts = rel.parts

    if rel == Path("."):
        return "Executable entry points, pipelines, services, and tests"

    # scripts/lib/X/ → label from directory name
    label = parts[-1].replace("_", " ").replace("-", " ").title()
    return label


def _files_in_dir(path: Path) -> list[str]:
    """List .py and .sh files in a directory (non-recursive)."""
    files = []
    for p in sorted(path.iterdir()):
        if p.suffix in (".py", ".sh") and p.suffix not in IGNORE_SUFFIXES:
            if "_archive" not in p.parts:
                files.append(p.name)
    return files


def _extract_file_info(fname: str, parent: Path) -> dict:
    """Extract status and purpose from file header.

    .py files: reads # Status: line and first docstring line.
    .sh files: reads first # comment line.
    """
    path = parent / fname
    try:
        text = path.read_text("utf-8")
    except Exception:
        return {"status": "unknown", "purpose": fname}

    lines = text.split("\n")
    info = {"status": "unknown", "purpose": fname}

    for i, line in enumerate(lines[:20]):
        if line.startswith("# Status:"):
            info["status"] = line.split(":", 1)[1].strip()
        if line.strip().startswith(('"""', "'''")):
            end = line.strip()[3:].strip()
            if end and not end.startswith(('"""', "'''")):
                info["purpose"] = end.rstrip('"').rstrip("'").strip()
                return info
            for j in range(i + 1, min(i + 5, len(lines))):
                end_line = lines[j].strip()
                if end_line.endswith(('"""', "'''")):
                    info["purpose"] = end_line[:-3].strip()
                    return info
                if end_line and not end_line.startswith(('"', "'")):
                    info["purpose"] = end_line.strip()
                    return info
            break

    if fname.endswith(".sh"):
        for line in lines[1:5]:
            if line.startswith("# ") and not line.startswith("# Status"):
                info["purpose"] = line.lstrip("# ").strip()
                break

    return info


def _config_key(dir_path: Path) -> str:
    """Normalised key for group matching (no trailing slash)."""
    rel = dir_path.relative_to(SCRIPTS_DIR)
    if rel == Path("."):
        return "scripts"
    return "scripts/" + str(rel)


def _scripts_hash() -> str:
    """Quick hash of scripts/ directory structure (files exist/removed, not content)."""
    import hashlib
    hasher = hashlib.md5()
    for d in sorted(_discover_dirs()):
        hasher.update(d.relative_to(SCRIPTS_DIR).as_posix().encode())
        for f in sorted(_files_in_dir(d)):
            hasher.update(f.encode())
    return hasher.hexdigest()


def _sync_code_structure() -> list[str]:
    """Regenerate all file entries from disk headers. Preserves group-level metadata."""
    if not CODE_STRUCTURE_YAML.exists():
        return ["[sync] code-structure.yaml not found — skip"]

    with open(CODE_STRUCTURE_YAML) as f:
        content = f.read()

    existing = yaml.safe_load(content)
    existing_modules = existing.get("modules", [])

    # Index existing modules by normalised path (no trailing slash)
    existing_by_path: dict[str, dict] = {}
    for mod in existing_modules:
        key = mod.get("path", "").rstrip("/")
        existing_by_path[key] = mod

    # Discover directories and build new module list
    dirs = _discover_dirs()
    new_modules: list[dict] = []

    for d in dirs:
        files = _files_in_dir(d)
        if not files:
            continue

        key = _config_key(d)
        existing_mod = existing_by_path.get(key)

        # Preserve existing purpose AND metadata; fallback to auto-inferred
        purpose = (existing_mod.get("purpose") if existing_mod
                   else _infer_group_purpose(d))

        module: dict = {
            "path": key + "/",
            "purpose": purpose,
            "files": {},
        }
        if existing_mod:
            for k in ("constraints", "allow_new_files", "no_subprocess", "import_restrictions",
                       "entry_points_only", "delegate_logic_to_lib", "max_lines"):
                if k in existing_mod:
                    module[k] = existing_mod[k]

        # Regenerate file entries from actual files on disk
        for fname in files:
            module["files"][fname] = _extract_file_info(fname, d)

        new_modules.append(module)

    # Drop modules that no longer have files on disk
    result = {"metadata": {"updated": datetime.now(KST).strftime("%Y-%m-%d")}, "modules": new_modules}

    raw = yaml.dump(result, default_flow_style=False, sort_keys=False)
    new_content = CODE_STRUCTURE_HEADER + raw

    changes = []
    if new_content != content:
        CODE_STRUCTURE_YAML.write_text(new_content)
        changes.append(f"[sync] code-structure.yaml regenerated ({len(new_modules)} groups, "
                       f"{sum(len(m['files']) for m in new_modules)} files)")
    else:
        changes.append("[sync] code-structure.yaml unchanged")

    return changes


# ── main ────────────────────────────────────────────────────────────

TIMER_UNIT_DIR = Path(os.path.expanduser("~/.config/systemd/user/"))
SYSTEM_TIMER_NAMES = {"logrotate", "mlocate-updatedb", "systemd-tmpfiles-clean", "dnf-makecache"}


def _get_timer_prop(unit_name: str, prop: str, scope: str = "user") -> str:
    """Get a single property from a systemd timer unit."""
    import subprocess
    try:
        cmd = (["systemctl", "--user"] if scope == "user" else []) + \
              ["show", f"{unit_name}.timer", "-p", prop, "--value"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        val = r.stdout.strip()
        return val if val != "n/a" else ""
    except Exception:
        return ""


def _parse_timers() -> dict:
    """Collect all systemd timers (user + relevant system) with schedule info."""
    import subprocess

    timers = {"user": [], "system": []}

    # Discover user timer unit names
    user_timer_names = []
    try:
        lines = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "--no-legend", "--type=timer"],
            capture_output=True, text=True, timeout=10
        ).stdout.strip().split("\n")
        for line in lines:
            if line.strip():
                name = line.strip().split()[0].replace(".timer", "")
                user_timer_names.append(name)
    except Exception:
        pass

    for tname in user_timer_names:
        if not (tname.startswith("devforge-") or tname in ("reference-monitor", "activity-summarizer", "grub-boot-success")):
            continue

        # Read OnCalendar from unit file directly (systemctl show may not expand it)
        unit_path = TIMER_UNIT_DIR / f"{tname}.timer"
        on_cal = ""
        if unit_path.exists():
            try:
                for uline in unit_path.read_text().split("\n"):
                    if uline.startswith("OnCalendar="):
                        on_cal = uline.split("=", 1)[1].strip()
            except Exception:
                pass

        next_ts = _get_timer_prop(tname, "NextElapseUSecRealtime")
        last_ts = _get_timer_prop(tname, "LastTriggerUSecRealtime")

        timers["user"].append({
            "unit": tname,
            "on_calendar": on_cal,
            "last": last_ts,
            "next": next_ts,
        })

    # System timers
    for tname in SYSTEM_TIMER_NAMES:
        on_cal = _get_timer_prop(tname, "OnCalendar", "system")
        if not on_cal:
            continue
        next_ts = _get_timer_prop(tname, "NextElapseUSecRealtime", "system")
        last_ts = _get_timer_prop(tname, "LastTriggerUSecRealtime", "system")
        timers["system"].append({
            "unit": tname,
            "on_calendar": on_cal,
            "last": last_ts,
            "next": next_ts,
        })

    return timers

    return timers


def _gen_timer_registry() -> str:
    """Generate timer-registry.yaml from live systemd data."""
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    timers = _parse_timers()

    doc = {
        "metadata": {
            "updated": now,
            "source": "live (systemctl list-timers)",
        },
        "user_timers": timers["user"],
        "system_timers": timers["system"],
    }

    header = (
        "# DevForge — Timer Registry\n"
        f"# auto-generated from live systemd data at {now}\n"
        "---\n"
    )
    return header + yaml.dump(doc, default_flow_style=False, sort_keys=False)

def _write_if_changed(path: Path, content: str, label: str) -> bool:
    if path.exists() and path.read_text() == content:
        print(f"[{label}] unchanged, skip")
        return False
    path.write_text(content)
    print(f"[{label}] written")
    return True


HASH_FILE = Path("/tmp/code-structure.hash")


def main():
    if "--check-structure" in sys.argv:
        h = _scripts_hash()
        if HASH_FILE.exists() and HASH_FILE.read_text() == h:
            print("[code-structure] unchanged, skip")
            return 0
        for msg in _sync_code_structure():
            print(msg)
        HASH_FILE.write_text(h)
        return 0

    from state_collector.main import collect_structural

    structural = collect_structural()

    with open(CLAUDE_YAML) as f:
        claude = yaml.safe_load(f)

    _write_if_changed(INFRA_OUTPUT, _gen_infrastructure(structural, claude), "infrastructure.md")
    ARCH_INFRA_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(ARCH_INFRA_OUTPUT, _gen_infrastructure(structural, claude), "architecture/infrastructure.md")
    SOFTWARE_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(SOFTWARE_OUTPUT, _build_software(structural, claude), "software.yaml")

    TIMER_REGISTRY_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    _write_if_changed(TIMER_REGISTRY_OUTPUT, _gen_timer_registry(), "timer-registry.yaml")

    # Auto-sync code-structure.yaml with actual files on disk
    for msg in _sync_code_structure():
        print(msg)


if __name__ == "__main__":
    main()
