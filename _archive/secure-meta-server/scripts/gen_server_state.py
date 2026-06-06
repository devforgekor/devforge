#!/usr/bin/env python3
"""gen_server_state.py — DevForge Server Knowledge Engine v4.2

Data collection -> state.yaml -> change detection -> changelog -> MOTD.
Single Python file. No Bash wrappers. No yq. Only dependency: pyyaml.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# ── Paths ──────────────────────────────────────────────────────────────
SERVER_DIR = Path("/opt/project/server")
STATE_FILE = SERVER_DIR / "state.yaml"
CHANGELOG_FILE = SERVER_DIR / "changelog.yaml"
ARCHIVE_FILE = SERVER_DIR / "changelog_archive.yaml"
CLAUDE_FILE = SERVER_DIR / "CLAUDE.yaml"
LAST_STRUCTURAL_HASH = SERVER_DIR / ".last-structural-hash"
MOTD_FILE = Path("/etc/motd")

TZ = timezone(timedelta(hours=9))
CHANGELOG_ARCHIVE_DAYS = 90

# ── ANSI ───────────────────────────────────────────────────────────────
GREEN = "\033[0;32m"
CYAN = "\033[0;36m"
YELLOW = "\033[1;33m"
RED = "\033[0;31m"
WHITE = "\033[0;37m"
BOLD = "\033[1m"
NC = "\033[0m"


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _run_lines(cmd, timeout=15):
    out = _run(cmd, timeout)
    return [l for l in out.split("\n") if l.strip()] if out else []


def discover_services():
    """Auto-discover tracked services from systemd user + system units.
    Returns list of (unit_name, display_name, scope) tuples. Scope: 'user' or 'system'."""
    services = []

    # User services: container-*.service + known names
    known_user = {"devforge-llm", "litellm", "netdata", "postgres"}
    for line in _run_lines(["systemctl", "--user", "list-unit-files", "--no-legend", "--type=service"]):
        name = line.strip().split()[0].replace(".service", "")
        if name.startswith("container-") or name in known_user:
            display = name.removeprefix("container-")
            services.append((name, display, "user"))

    # System services (non-container or rootful containers)
    known_system = {"caddy"}
    for line in _run_lines(["systemctl", "list-unit-files", "--no-legend", "--type=service"]):
        name = line.strip().split()[0].replace(".service", "")
        if name in known_system:
            services.append((name, name, "system"))

    return services


def collect_container_flags(name):
    """Parse llama-server flags from a podman systemd user unit file.
    Only captures flags AFTER the image name (podman operational flags are excluded)."""
    unit_path = os.path.expanduser(f"~/.config/systemd/user/{name}.service")
    if not os.path.exists(unit_path):
        return ""
    content = Path(unit_path).read_text()
    # Match ExecStart= line that spans multiple lines with backslash continuation
    match = re.search(r"ExecStart=/usr/bin/podman run\s+(.+?)(?:^[A-Z]\S+=|\Z)", content, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    args_block = match.group(1)
    args_block = args_block.replace("\\\n", " ").replace("\n", " ").strip()
    # Split on the image reference to separate podman flags from server flags
    # Image pattern: docker.io/... or ghcr.io/...
    image_match = re.search(r'\S+\.io/\S+:\S+', args_block)
    if not image_match:
        return ""
    server_args = args_block[image_match.end():].strip()
    flags = []
    try:
        tokens = shlex.split(server_args)
        i = 0
        while i < len(tokens):
            if tokens[i].startswith("-"):
                flags.append(tokens[i])
                # Capture value if next token is not a flag
                if i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                    flags.append(tokens[i + 1])
                    i += 1
            i += 1
    except ValueError:
        return ""
    return " ".join(flags)


def build_memory_line():
    """Generate memory overview line from live system data for CLAUDE.yaml."""
    # RAM
    mem_total = "?"
    for line in _run(["free", "-h"]).split("\n"):
        if line.startswith("Mem:"):
            mem_total = line.split()[1]
            break

    # Swap
    swap_total = 0
    for line in _run_lines(["swapon", "--show", "--noheadings", "--bytes"]):
        parts = line.split()
        if len(parts) >= 3:
            swap_total += int(parts[2])
    swap_str = f"{swap_total / (1024**3):.1f}G" if swap_total else "0G"
    if swap_str.endswith(".0G"):
        swap_str = swap_str.replace(".0G", "G")

    # Zram
    zram = ""
    zram_lines = _run_lines(["zramctl"])
    if len(zram_lines) >= 2:
        parts = zram_lines[1].split()
        if len(parts) >= 5:
            zram = f" + {parts[2]} zram ({parts[4]})"

    # Swappiness
    swappiness = _run(["sysctl", "-n", "vm.swappiness"]).strip() or "?"

    return f"{mem_total}{zram} + {swap_str} swap (swappiness={swappiness})"


# ═══════════════════════════════════════════════════════════════════════
# COLLECT
# ═══════════════════════════════════════════════════════════════════════

def collect_structural():
    data = {}

    # Storage (LVM)
    storage = []
    for line in _run_lines(["sudo", "lvs", "--noheadings", "-o", "vg_name,lv_name,lv_size", "--units", "g"]):
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        vg, lv, sz_raw = parts[0], parts[1], parts[2].rstrip("g")
        sz_val = float(sz_raw)
        sz = f"{int(sz_val)}G" if sz_val == int(sz_val) else f"{sz_val}G"
        device = f"{vg}-{lv}"
        mnt = _run(["findmnt", "-n", "-o", "TARGET", f"/dev/mapper/{device}"])
        storage.append({
            "device": device, "vg": vg, "lv": lv, "size": sz,
            "mounted": bool(mnt), "mount": mnt or None,
        })
    data["storage"] = storage

    # Containers (rootless + rootful)
    containers = []
    for cmd in [["podman", "ps"], ["sudo", "-n", "podman", "ps"]]:
        for line in _run_lines(cmd + ["--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"]):
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            flags = collect_container_flags(parts[0])
            # Only extract model for containers that have a -m flag (gguf path)
            model = None
            if flags and "-m " in flags:
                model_match = re.search(r"-m\s+(\S+)", flags)
                if model_match:
                    model = Path(model_match.group(1)).name
            containers.append({
                "name": parts[0], "status": parts[1],
                "ports": parts[2] if len(parts) > 2 else "",
                "model": model,
                "flags": flags,
            })
    data["containers"] = containers

    # Services (auto-discovered)
    services = []
    for unit, display, scope in discover_services():
        if scope == "user":
            status = _run(["systemctl", "--user", "is-active", f"{unit}.service"])
            enabled = _run(["systemctl", "--user", "is-enabled", f"{unit}.service"])
        else:
            status = _run(["systemctl", "is-active", f"{unit}.service"])
            enabled = _run(["systemctl", "is-enabled", f"{unit}.service"])
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
    for line in _run_lines(["free"]):
        if line.startswith("Mem:"):
            parts = line.split()
            mem_total = int(parts[1])
            mem_used = int(parts[2])
            mem_pct = round(mem_used / mem_total * 100) if mem_total else 0
            break

    # CPU load
    cpu_load = []
    uptime_out = _run(["uptime"])
    if "load average" in uptime_out:
        loads = uptime_out.split("load average:")[-1].strip()
        cpu_load = [float(l.strip()) for l in loads.split(",")]

    # Zram
    zram = ""
    lines = _run_lines(["zramctl"])
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 5:
            zram = f"{parts[2]} {parts[4]}"

    data["system"] = {
        "cpu_load": cpu_load,
        "memory": {"used": mem_h_used, "total": mem_h_total, "percent": mem_pct},
        "zram": zram,
    }

    # Storage usage
    storage_use = []
    for line in _run_lines(["df", "-h"]):
        if "/dev/mapper/" not in line:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        storage_use.append({
            "device": parts[0].replace("/dev/mapper/", ""),
            "size": parts[1], "used": parts[2], "avail": parts[3],
            "use_pct": int(parts[4].rstrip("%")), "mount": parts[5],
        })
    data["storage_use"] = storage_use

    # Container uptime
    containers_uptime = []
    for line in _run_lines(["podman", "ps", "--format", "{{.Names}}\t{{.Status}}"]):
        parts = line.split("\t")
        if len(parts) >= 2:
            containers_uptime.append({"name": parts[0], "uptime": parts[1]})
    data["containers_uptime"] = containers_uptime

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
    # Structural alerts from collect_structural data
    data["alerts"] = alerts

    return data


# ═══════════════════════════════════════════════════════════════════════
# STATE I/O
# ═══════════════════════════════════════════════════════════════════════

def save_state(structural, metrics):
    doc = {"structural": structural, "metrics": metrics}
    STATE_FILE.write_text(yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


def load_previous_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return yaml.safe_load(f) or {}
    return None


# ═══════════════════════════════════════════════════════════════════════
# CHANGE DETECTION
# ═══════════════════════════════════════════════════════════════════════

def structural_hash(data):
    payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def diff_structural(prev, curr):
    changes = []

    def _compare(prefix, a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            for k in sorted(set(a.keys()) | set(b.keys())):
                _compare(f"{prefix}.{k}", a.get(k), b.get(k))
        elif isinstance(a, list) and isinstance(b, list):
            a_idx = {}
            b_idx = {}
            for item in a:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("device") or json.dumps(item, sort_keys=True)
                    a_idx[key] = item
            for item in b:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("device") or json.dumps(item, sort_keys=True)
                    b_idx[key] = item
            for k in sorted(set(a_idx.keys()) | set(b_idx.keys())):
                if k not in a_idx:
                    changes.append({"path": f"{prefix}[{k}]", "type": "added", "after": b_idx[k]})
                elif k not in b_idx:
                    changes.append({"path": f"{prefix}[{k}]", "type": "removed", "before": a_idx[k]})
                else:
                    _compare(f"{prefix}[{k}]", a_idx[k], b_idx[k])
        elif a != b:
            changes.append({"path": prefix, "type": "changed", "before": a, "after": b})

    _compare("structural", prev, curr)
    return changes


# ═══════════════════════════════════════════════════════════════════════
# CHANGELOG
# ═══════════════════════════════════════════════════════════════════════

def load_changelog():
    if CHANGELOG_FILE.exists():
        with open(CHANGELOG_FILE) as f:
            return yaml.safe_load(f) or {}
    return {"entries": []}


def save_changelog(data):
    CHANGELOG_FILE.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


def append_changelog_entry(changes):
    data = load_changelog()
    summary_parts = []
    for c in changes[:5]:
        path = c["path"]
        if c["type"] == "changed":
            summary_parts.append(f"{path}: {c['before']} -> {c['after']}")
        elif c["type"] == "added":
            summary_parts.append(f"{path}: added")
        elif c["type"] == "removed":
            summary_parts.append(f"{path}: removed")
    summary = "; ".join(summary_parts)
    if len(changes) > 5:
        summary += f" (+{len(changes) - 5} more)"

    entry = {
        "time": datetime.now(TZ).isoformat(),
        "type": "auto",
        "summary": summary,
        "changed": changes,
        "decisions": [],
    }
    data["entries"].insert(0, entry)
    save_changelog(data)
    archive_old_entries()


def archive_old_entries():
    cutoff = datetime.now(TZ) - timedelta(days=CHANGELOG_ARCHIVE_DAYS)
    data = load_changelog()

    active, archived = [], []
    for entry in data.get("entries", []):
        try:
            t = datetime.fromisoformat(entry.get("time", ""))
        except (ValueError, TypeError):
            active.append(entry)
            continue
        if t < cutoff:
            archived.append(entry)
        else:
            active.append(entry)

    if not archived:
        return

    data["entries"] = active
    save_changelog(data)

    archive_data = {"entries": []}
    if ARCHIVE_FILE.exists():
        with open(ARCHIVE_FILE) as f:
            archive_data = yaml.safe_load(f) or {"entries": []}
    archive_data["entries"] = archived + archive_data.get("entries", [])
    ARCHIVE_FILE.write_text(yaml.dump(archive_data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


# ═══════════════════════════════════════════════════════════════════════
# CLAUDE.YAML AUTO-UPDATE
# ═══════════════════════════════════════════════════════════════════════

def _load_yaml(path):
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return None


def _save_yaml(path, data):
    path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120))


def update_claude_yaml(structural):
    """Auto-update storage/services/network in CLAUDE.yaml from live data.
    Preserves manual sections: overview, rules, entry_points, tracked, handover."""
    claude = _load_yaml(CLAUDE_FILE)
    if not claude:
        return

    # ── Storage ──────────────────────────────────────────────────────
    storage_list = structural.get("storage", [])
    vgs = {}
    for s in storage_list:
        vg = s["vg"]
        if vg not in vgs:
            vgs[vg] = {"lvs": [], "total_lv": 0.0}
        vgs[vg]["lvs"].append({
            "lv": s["lv"],
            "size": s["size"],
            "mount": s.get("mount") or "unmounted",
        })
        sz_str = s["size"].rstrip("G")
        vgs[vg]["total_lv"] += float(sz_str)

    for line in _run_lines(["sudo", "vgs", "--noheadings", "-o", "vg_name,vg_size", "--units", "g"]):
        parts = line.strip().split()
        if len(parts) >= 2 and parts[0] in vgs:
            vg_total = float(parts[1].rstrip("g"))
            free = vg_total - vgs[parts[0]]["total_lv"]
            vgs[parts[0]]["free"] = f"{free:.1f}G" if free != int(free) else f"{int(free)}G"

    old_storage = claude.get("storage", {})
    for disk_key, disk_data in old_storage.items():
        vg = disk_data.get("vg")
        if vg and vg in vgs:
            disk_data["lvs"] = vgs[vg]["lvs"]
            disk_data["free"] = vgs[vg].get("free", disk_data.get("free", "?"))

    # ── Services (update status, preserve type/hand-written fields) ──
    new_services = structural.get("services", [])
    old_services = claude.get("services", [])
    old_by_name = {s["name"]: s for s in old_services}
    merged = []
    for ns in new_services:
        entry = {"name": ns["name"], "status": ns["status"]}
        old = old_by_name.get(ns["name"], {})
        if "type" in old:
            entry["type"] = old["type"]
        merged.append(entry)
    # Preserve manually-added services not in live data
    for name, old in old_by_name.items():
        if name not in {ns["name"] for ns in new_services}:
            merged.append(old)
    claude["services"] = merged

    # ── Network (update ip only, preserve the rest) ──────────────────
    new_net = structural.get("network", {})
    old_net = claude.get("network", {})
    if old_net and new_net.get("ip"):
        old_net["ip"] = new_net["ip"]
    claude["network"] = old_net

    # ── Memory (auto-generated from live data) ─────────────────────────
    claude["overview"]["memory"] = build_memory_line()

    # ── Container flags (sync from live unit files) ────────────────────
    new_containers = structural.get("containers", [])
    old_containers = claude.get("containers", {})
    if new_containers and isinstance(old_containers, dict):
        flags_list = []
        for c in new_containers:
            if c.get("flags"):
                flags_list.append(c["flags"])
        if flags_list:
            old_containers["flags"] = flags_list[0]  # primary container flags
        claude["containers"] = old_containers

    _save_yaml(CLAUDE_FILE, claude)


# ═══════════════════════════════════════════════════════════════════════
# VALIDATION
# ═══════════════════════════════════════════════════════════════════════


def run_validation(structural):
    """Compare CLAUDE.yaml + blueprint.yaml claims against live data.
    Returns (status, mismatches) where status is 'pass' or 'fail'."""
    mismatches = []
    claude = _load_yaml(CLAUDE_FILE) or {}
    blueprint = _load_yaml(SERVER_DIR / "blueprint.yaml") or {}

    # 1. CLAUDE.yaml services vs live systemd
    claude_services = {s["name"]: s.get("status") for s in claude.get("services", [])}
    for s in structural.get("services", []):
        claude_status = claude_services.get(s["name"])
        if claude_status and claude_status != s["status"]:
            mismatches.append({
                "item": f"CLAUDE.yaml service {s['name']}",
                "expected": claude_status,
                "actual": s["status"],
            })

    # 2. CLAUDE.yaml memory vs live
    live_memory = build_memory_line()
    claude_memory = claude.get("overview", {}).get("memory", "")
    if claude_memory and claude_memory != live_memory:
        mismatches.append({
            "item": "CLAUDE.yaml overview.memory",
            "expected": claude_memory,
            "actual": live_memory,
        })

    # 3. CLAUDE.yaml container flags vs live unit file (only for llama-server container)
    claude_flags = claude.get("containers", {}).get("flags", "")
    if claude_flags:
        for c in structural.get("containers", []):
            if not c.get("model"):
                continue  # skip non-llm containers
            live_flags = collect_container_flags(c["name"])
            if live_flags and claude_flags != live_flags:
                mismatches.append({
                    "item": f"CLAUDE.yaml containers.flags ({c['name']})",
                    "expected": claude_flags,
                    "actual": live_flags,
                })

    # 4. CLAUDE.yaml model vs actual gguf file (strip size annotation like "(8.1GB)")
    claude_model = claude.get("containers", {}).get("model", "")
    claude_model_base = re.sub(r"\s*\([^)]*\)", "", claude_model).strip()
    if claude_model_base:
        for c in structural.get("containers", []):
            live_model = c.get("model", "")
            if live_model and claude_model_base != live_model:
                mismatches.append({
                    "item": "CLAUDE.yaml containers.model",
                    "expected": claude_model,
                    "actual": live_model,
                })

    # 5. Listening ports — check that claude network claims match actual ports
    live_ports = structural.get("network", {}).get("ports", [])  # list of port strings
    claude_llm = claude.get("network", {}).get("llm_api", "")
    if "8080" in claude_llm and "8080" not in live_ports:
        mismatches.append({
            "item": "CLAUDE.yaml network.llm_api claims port 8080",
            "expected": "port 8080 listening",
            "actual": f"listening ports: {live_ports}",
        })

    # 6. Blueprint Phase completed items vs live
    for phase in blueprint.get("phases", []):
        if phase.get("status") != "complete":
            continue
        for item in (phase.get("completed") or []):
            # ctx-size claim (only check containers that actually run a model)
            ctx_match = re.search(r"ctx-size (\d+)", item)
            if ctx_match:
                claimed_ctx = ctx_match.group(1)
                for c in structural.get("containers", []):
                    if not c.get("model"):
                        continue
                    live_flags = collect_container_flags(c["name"])
                    if f"--ctx-size {claimed_ctx}" not in live_flags:
                        actual_ctx = re.search(r"--ctx-size (\d+)", live_flags)
                        mismatches.append({
                            "item": f"blueprint Phase {phase['phase']}: ctx-size {claimed_ctx}",
                            "expected": f"--ctx-size {claimed_ctx}",
                            "actual": f"--ctx-size {actual_ctx.group(1)}" if actual_ctx else "not found",
                        })
            # Mount claim (strip trailing punctuation from regex capture)
            mnt_match = re.search(r"mounted at (\S+)", item)
            if mnt_match:
                mnt_path = mnt_match.group(1).rstrip(",;")
                if not _run(["findmnt", mnt_path]):
                    mismatches.append({
                        "item": f"blueprint Phase {phase['phase']}: {mnt_path} mounted",
                        "expected": "mounted",
                        "actual": "not mounted",
                    })

    status = "fail" if mismatches else "pass"
    return status, mismatches


# ═══════════════════════════════════════════════════════════════════════
# MOTD
# ═══════════════════════════════════════════════════════════════════════

def generate_motd(structural, metrics):
    lines = []
    today = datetime.now(TZ).strftime("%Y.%m.%d.")

    lines.append(f"{CYAN}{'='*64}{NC}")
    lines.append(f"  {YELLOW}DevForge Server Env [{today}]{NC}")
    lines.append(f"{CYAN}{'='*64}{NC}")

    # System
    sys_m = metrics.get("system", {})
    mem = sys_m.get("memory", {})
    cpu = sys_m.get("cpu_load", [])
    zram = sys_m.get("zram", "N/A")
    cpu_str = ", ".join(f"{v:.2f}" for v in cpu) if cpu else "N/A"

    cpu_color = WHITE
    if cpu and cpu[0] >= 3.5:
        cpu_color = RED
    elif cpu and cpu[0] >= 2.5:
        cpu_color = YELLOW

    mem_pct = mem.get("percent", 0)
    mem_color = WHITE
    if mem_pct >= 90:
        mem_color = RED
    elif mem_pct >= 80:
        mem_color = YELLOW

    lines.append(f"{GREEN}[SYSTEM]{NC}  OCI ARM Flex 4ocpu 24ram | Oracle Linux Server 9.7 | aarch64")
    lines.append(f"        {cpu_color}CPU Load: {cpu_str:<22}{NC} {mem_color}Mem: {mem.get('used','?')}/{mem.get('total','?') + ' ': <18}{NC} Zram: {zram if zram else 'none'}")

    # Storage — LVM
    lines.append(f"\n{GREEN}[STORAGE — LVM]{NC}")
    lines.append(f"  {BOLD}{'VG':<12} {'LV':<12} {'Size':>6}  Mount{NC}")
    for s in structural.get("storage", []):
        color = YELLOW if not s.get("mounted") else WHITE
        lines.append(f"  {color}{s['vg']:<12} {s['lv']:<12} {s['size']:>6}  {s.get('mount') or '(unmounted)'}{NC}")

    lines.append(f"\n  {BOLD}{'Device':<22} {'Size':>5} {'Used':>5} {'Avail':>5} {'Use%':>4} Mounted{NC}")
    for s in metrics.get("storage_use", []):
        pct = s.get("use_pct", 0)
        if pct >= 90:
            color = RED
        elif pct >= 80:
            color = YELLOW
        else:
            color = WHITE
        lines.append(f"  {color}{s['device']:<22} {s['size']:>5} {s['used']:>5} {s['avail']:>5} {s['use_pct']:>3}% {s['mount']}{NC}")

    # Containers
    lines.append(f"\n{GREEN}[CONTAINERS]{NC}")
    containers = structural.get("containers", [])
    if containers:
        lines.append(f"  {'NAMES':<12} {'STATUS':<22} {'PORTS'}")
        for c in containers:
            status = c.get("status", "")
            if "unhealthy" in status.lower():
                color = RED
            elif "healthy" in status.lower() or "Up" in status:
                color = WHITE
            else:
                color = WHITE
            lines.append(f"  {color}{c['name']:<12} {status:<22} {c.get('ports', '')}{NC}")
        model = containers[0].get("model") if containers else None
        if model:
            lines.append(f"\n  Active Model: {YELLOW}{model}{NC}")
    else:
        lines.append("  (no containers running)")

    # Network
    net = structural.get("network", {})
    lines.append(f"\n{GREEN}[NETWORK]{NC}")
    llm_port = next((c.get("ports", "") for c in containers if c.get("name") == "devforge-llm"), "")
    litellm_port = next((c.get("ports", "") for c in containers if c.get("name") == "litellm"), "")
    lines.append(f"  IP: {net.get('ip', 'N/A')} | SSH: 22 | LLM: {llm_port or '8080'} | LiteLLM: {litellm_port or '4000'}")

    # Services
    lines.append(f"\n{GREEN}[SERVICES]{NC}")
    for svc in structural.get("services", []):
        status = svc.get("status", "unknown")
        if status == "active":
            color = WHITE
        elif status == "inactive":
            color = YELLOW
        else:
            color = RED
        lines.append(f"  {svc['name']}: {color}{status}{NC}")

    lines.append(f"{CYAN}{'='*64}{NC}")

    # Validation alerts
    validation = metrics.get("validation")
    if validation and validation.get("status") == "fail":
        lines.append(f"\n{RED}[VALIDATION ALERTS — {validation.get('last_run', '?')}]{NC}")
        for m in validation.get("mismatches", []):
            lines.append(f"  {RED}[MISMATCH]{NC} {m['item']}")
            lines.append(f"          expected: {YELLOW}{m['expected']}{NC}  actual: {RED}{m['actual']}{NC}")
        lines.append(f"{RED}  Fix CLAUDE.yaml or blueprint.yaml and re-run --validate{NC}")

    MOTD_FILE.write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════

def main():
    validate_mode = "--validate" in sys.argv

    # 1. Load previous state BEFORE overwriting (for diff)
    prev_state = load_previous_state()
    prev_structural = prev_state.get("structural") if prev_state else None

    # 2. Collect fresh data
    structural = collect_structural()
    metrics = collect_metrics()

    # ── Validate-only mode: compare, write alerts, exit ────────────────
    if validate_mode:
        status, mismatches = run_validation(structural)
        now_str = datetime.now(TZ).isoformat()
        validation = {
            "last_run": now_str,
            "status": status,
            "mismatches": mismatches,
        }
        # Write validation result into state.yaml (merge with existing)
        prev = _load_yaml(STATE_FILE) or {}
        prev["validation"] = validation
        _save_yaml(STATE_FILE, prev)
        # Also update MOTD so it's visible immediately
        prev.setdefault("structural", structural)
        prev.setdefault("metrics", metrics)
        prev["metrics"]["validation"] = validation
        generate_motd(prev.get("structural", structural), prev.get("metrics", metrics))
        return 0 if status == "pass" else 1

    # 3. Save new state
    save_state(structural, metrics)

    # 4. Detect changes against previous structural state
    new_hash = structural_hash(structural)

    if prev_structural is None:
        # First run
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
                    "motd-gen.timer triggers every 15min",
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
    update_claude_yaml(structural)

    # 6. Preserve validation results from previous state and include in MOTD
    prev_validation = (prev_state or {}).get("validation")
    if prev_state and prev_validation:
        metrics["validation"] = prev_validation
        # Re-save with validation preserved
        save_state(structural, metrics)

    # 7. Generate MOTD
    generate_motd(structural, metrics)

    return 0


if __name__ == "__main__":
    sys.exit(main())
