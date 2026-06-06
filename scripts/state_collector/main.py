#!/usr/bin/env python3
# Status: production
# Path: none — library
"""state_collector — DevForge Server Knowledge Engine v4.4

Thin orchestrator: collect → detect changes → update docs → MOTD.
Heavy lifting delegated to lib/ (state, infra, output).
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.db import psql as _psql, get_token_stats
from lib.infra.containers import discover_services, collect_container_flags
from lib.infra.storage import track_zram_cycles
from lib.infra.subprocess import run_subprocess, run_lines as _run_lines
from lib.output.claude_yaml import update_claude_yaml
from lib.output.motd import generate_motd
from lib.output.validation import run_validation
from lib.output.yaml_io import load_yaml as _load_yaml, save_yaml as _save_yaml
from lib.state.changelog import load_changelog, save_changelog, append_changelog_entry
from lib.state.diff import structural_hash, diff_structural
from lib.state.state_io import save_state, load_previous_state

SERVER_DIR = Path("/opt/projects/server")
STATE_FILE = SERVER_DIR / "state.yaml"
CLAUDE_FILE = SERVER_DIR / "CLAUDE.yaml"
LAST_STRUCTURAL_HASH = SERVER_DIR / ".last-structural-hash"
MOTD_FILE = Path("/opt/projects/server/data/motd_daily.txt")
ZRAM_STATE_FILE = Path.home() / ".cache/devforge/zram-state.json"

TZ = timezone.utc
TOKEN_USAGE_BASE = 1_000_000


# COLLECT

def collect_structural():
    data = {}

    # Storage (LVM)
    storage = []
    for line in _run_lines(["sudo", "lvs", "--noheadings", "-o", "vg_name,lv_name,lv_size", "--units", "g"]):
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        vg, lv, sz_raw = parts[0], parts[1], parts[2].rstrip("g")
        try:
            sz_val = float(sz_raw.lstrip("<"))
            sz = f"{int(sz_val)}G" if sz_val == int(sz_val) else f"{sz_val}G"
        except ValueError:
            sz = parts[2]
        device = f"{vg}-{lv}"
        dm_path = Path(f"/dev/mapper/{device}")
        mnt = run_subprocess(["findmnt", "-n", "-o", "TARGET", str(dm_path)]).split("\n")[0]
        resolved = str(dm_path.resolve()) if dm_path.exists() else ""
        swap_lines = _run_lines(["swapon", "--show", "--noheadings"])
        is_swap = resolved and any(l.startswith(f"{resolved} ") for l in swap_lines)
        if not mnt and is_swap:
            mnt = "SWAP"
        storage.append({
            "device": device, "vg": vg, "lv": lv, "size": sz,
            "mounted": bool(mnt), "mount": mnt or None,
        })
    data["storage"] = storage

    # Containers (rootless + rootful)
    containers = []
    pod_map = {}
    for line in _run_lines(["podman", "pod", "ps", "--format", "{{.Name}}\t{{.ID}}"]):
        parts = line.split("\t")
        if len(parts) >= 2:
            pod_map[parts[1][:12]] = parts[0]

    for cmd in [["podman", "ps"], ["sudo", "-n", "podman", "ps"]]:
        for line in _run_lines(cmd + ["--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}\t{{.Image}}"]):
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            if parts[0].endswith("-infra"):
                pod_id = parts[0].split("-")[0]
                pod_name = pod_map.get(pod_id[:12], pod_id[:8])
                parts[0] = f"pod:{pod_name}"
            flags = collect_container_flags(parts[0])
            model = None
            if flags and "-m " in flags:
                model_match = re.search(r"-m\s+(\S+)", flags)
                if model_match:
                    model = Path(model_match.group(1)).name
            containers.append({
                "name": parts[0], "status": parts[1],
                "ports": parts[2] if len(parts) > 2 else "",
                "image": parts[3] if len(parts) > 3 else "",
                "model": model,
                "flags": flags,
            })
    data["containers"] = containers

    # Services (auto-discovered)
    services = []
    for unit, display, scope in discover_services():
        if scope == "user":
            status = run_subprocess(["systemctl", "--user", "is-active", f"{unit}.service"])
            enabled = run_subprocess(["systemctl", "--user", "is-enabled", f"{unit}.service"])
        else:
            status = run_subprocess(["systemctl", "is-active", f"{unit}.service"])
            enabled = run_subprocess(["systemctl", "is-enabled", f"{unit}.service"])
        if not enabled or "Failed" in enabled:
            status = "not-installed"
        services.append({"name": display, "status": status})
    data["services"] = services

    # Network
    ip = ""
    for line in _run_lines(["ip", "-4", "addr", "show", "enp0s6"]):
        m = re.search(r"inet\s+(\S+)", line)
        if m:
            ip = m.group(1)
            break

    ports = []
    for line in _run_lines(["ss", "-tlnp"]):
        if "LISTEN" not in line:
            continue
        parts = line.strip().split()
        if len(parts) >= 4:
            port = parts[3].rsplit(":", 1)[-1]
            if port.isdigit():
                ports.append(port)
    data["network"] = {"ip": ip, "ports": sorted(set(ports))}

    return data


def collect_metrics():
    data = {"generated": datetime.now(TZ).isoformat()}

    # Memory
    mem_total = mem_used = mem_pct = 0
    mem_h_total = mem_h_used = ""

    for line in _run_lines(["free", "-h"]):
        if line.startswith("Mem:"):
            parts = line.split()
            mem_h_total, mem_h_used = parts[1], parts[2]
            break

    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_used = mem_total - int(line.split()[1])
            if mem_total:
                mem_pct = round(mem_used / mem_total * 100)
    except Exception:
        for line in _run_lines(["free"]):
            if line.startswith("Mem:"):
                parts = line.split()
                mem_total = int(parts[1])
                mem_used = int(parts[2])
                mem_pct = round(mem_used / mem_total * 100) if mem_total else 0
                break

    cpu_load = []
    uptime_out = run_subprocess(["uptime"])
    if "load average" in uptime_out:
        loads = uptime_out.split("load average:")[-1].strip()
        cpu_load = [float(l.strip()) for l in loads.split(",")]

    cpu_cores = os.cpu_count() or 4

    # Zram (with daily cycle tracking)
    zram = ""
    zram_cycles = 0
    zram_total_cycles = 0
    current_active, zram_cycles, zram_total_cycles, zram_parts = track_zram_cycles(ZRAM_STATE_FILE)
    if zram_parts and len(zram_parts) >= 5:
        zram = f"{zram_parts[3]}/{zram_parts[2]}"

    data["system"] = {
        "cpu_load": cpu_load,
        "cpu_cores": cpu_cores,
        "memory": {"used": mem_h_used, "total": mem_h_total, "percent": mem_pct},
        "zram": zram,
        "zram_cycles": zram_cycles,
        "zram_total_cycles": zram_total_cycles,
    }

    # Storage usage
    storage_use = []
    for line in _run_lines(["df", "-h"]):
        if "/dev/mapper/" not in line:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            use_pct = int(parts[4].rstrip("%"))
        except ValueError:
            use_pct = 0
        storage_use.append({
            "device": parts[0].replace("/dev/mapper/", ""),
            "size": parts[1], "used": parts[2], "avail": parts[3],
            "use_pct": use_pct, "mount": parts[5],
        })
    data["storage_use"] = storage_use

    # Container uptime
    containers_uptime = []
    for line in _run_lines(["podman", "ps", "--format", "{{.Names}}\t{{.Status}}"]):
        parts = line.split("\t")
        if len(parts) >= 2:
            containers_uptime.append({"name": parts[0], "uptime": parts[1]})
    data["containers_uptime"] = containers_uptime

    # Token usage
    data["tokens"] = get_token_stats() or {
        "session": {"turns": 0, "total_tokens": 0, "avg_tokens": 0},
        "total": {"turns": 0, "total_tokens": 0, "avg_tokens": 0},
    }

    # Alerts
    alerts = []
    for s in storage_use:
        pct = s["use_pct"]
        if pct >= 90:
            alerts.append({"level": "critical", "item": f"{s['device']} {pct}% full"})
        elif pct >= 80:
            alerts.append({"level": "warning", "item": f"{s['device']} {pct}% full"})
    if mem_pct >= 90:
        alerts.append({"level": "critical", "item": f"memory {mem_pct}%"})
    elif mem_pct >= 80:
        alerts.append({"level": "warning", "item": f"memory {mem_pct}%"})
    data["alerts"] = alerts

    return data


# ORCHESTRATION

def main():
    validate_mode = "--validate" in sys.argv

    # 1. Load previous state BEFORE overwriting (for diff)
    prev_state = load_previous_state(STATE_FILE)
    prev_structural = prev_state.get("structural") if prev_state else None

    # 2. Collect fresh data
    structural = collect_structural()

    metrics = collect_metrics()
    try:
        from lib.tracking.dependency_tracker import collect as collect_references
        references = collect_references()
        _psql(
            'CREATE TABLE IF NOT EXISTS "references" ('
            '  id SERIAL PRIMARY KEY,'
            '  url TEXT NOT NULL UNIQUE,'
            '  name TEXT NOT NULL,'
            '  category TEXT,'
            '  latest_version TEXT,'
            '  usage_count INT DEFAULT 0,'
            '  source_file TEXT,'
            '  first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),'
            '  last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()'
            ');'
            'CREATE INDEX IF NOT EXISTS idx_references_url ON "references"(url);'
            'CREATE INDEX IF NOT EXISTS idx_references_last ON "references"(last_seen DESC);'
        )
        for name, ref in references.items():
            _psql(
                f'INSERT INTO "references" (url, name, category, latest_version, usage_count) '
                f"VALUES ('{ref['url']}', '{name}', '{ref['category']}', "
                f"'{ref['external']['latest']}', {ref['internal']['usage_count']}) "
                f"ON CONFLICT (url) DO UPDATE SET "
                f"  latest_version = EXCLUDED.latest_version, "
                f"  usage_count = EXCLUDED.usage_count, "
                f"  last_seen = NOW()"
            )
    except Exception as e:
        print(f"  [refs] collection failed: {e}", file=sys.stderr)
        references = {}

    # Phase auto-tracking
    phase_data = {}
    try:
        from lib.tracking.phase_tracker import auto_update as auto_update_phases
        phase_data = auto_update_phases()
    except Exception as e:
        print(f"  [phase_tracker] auto-update failed: {e}", file=sys.stderr)

    if validate_mode:
        status, mismatches = run_validation(structural, CLAUDE_FILE, SERVER_DIR)
        now_str = datetime.now(TZ).isoformat()
        validation = {
            "last_run": now_str,
            "status": status,
            "mismatches": mismatches,
        }
        prev = _load_yaml(STATE_FILE) or {}
        prev["validation"] = validation
        _save_yaml(STATE_FILE, prev)
        prev.setdefault("structural", structural)
        prev.setdefault("metrics", metrics)
        prev["metrics"]["validation"] = validation
        generate_motd(prev.get("structural", structural), prev.get("metrics", metrics), MOTD_FILE, TOKEN_USAGE_BASE)
        return 0 if status == "pass" else 1

    # 3. Save new state
    save_state(structural, metrics, STATE_FILE, references, phase_data)

    # 4. Detect changes against previous structural state
    new_hash = structural_hash(structural)

    if prev_structural is None:
        data = load_changelog()
        if not data.get("entries"):
            data["entries"] = [{
                "time": datetime.now(TZ).isoformat(),
                "type": "bootstrap",
                "summary": "Server Knowledge System initialized — first structural snapshot",
                "changed": [{"path": "structural", "type": "initial",
                             "note": "Full initial state recorded in state.yaml"}],
                "decisions": [
                    "gen_server_state.py v4.2 deployed",
                    "motd generated dynamically on SSH login via pam_motd",
                    "CLAUDE.yaml is the machine entry point",
                ],
            }]
            save_changelog(data)
        LAST_STRUCTURAL_HASH.write_text(new_hash + "\n")
    else:
        prev_hash = structural_hash(prev_structural)
        if new_hash != prev_hash:
            changes = diff_structural(prev_structural, structural)
            if changes:
                append_changelog_entry(changes)
        LAST_STRUCTURAL_HASH.write_text(new_hash + "\n")

    # 5. Update CLAUDE.yaml dynamic sections
    update_claude_yaml(structural, CLAUDE_FILE)

    # 6. Preserve validation results from previous state and include in MOTD
    prev_validation = (prev_state or {}).get("validation")
    if prev_state and prev_validation:
        metrics["validation"] = prev_validation
        save_state(structural, metrics, STATE_FILE, references, phase_data)

    # 7. Generate MOTD
    generate_motd(structural, metrics, MOTD_FILE, TOKEN_USAGE_BASE)

    return 0


if __name__ == "__main__":
    sys.exit(main())
