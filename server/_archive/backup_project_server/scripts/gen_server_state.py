#!/usr/bin/env python3
"""gen_server_state.py — DevForge Server Knowledge Engine v4.2

Data collection -> state.yaml -> change detection -> changelog -> MOTD.
Single Python file. No Bash wrappers. No yq. Only dependency: pyyaml.
"""

import hashlib
import json
import os
import re
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

    # Containers
    containers = []
    for line in _run_lines(["podman", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Ports}}"]):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        model = _run(["bash", "-c", "find /ai-llm/models /opt/ai_data/models -name '*.gguf' 2>/dev/null | head -1 | xargs basename 2>/dev/null"])
        containers.append({
            "name": parts[0], "status": parts[1],
            "ports": parts[2] if len(parts) > 2 else "",
            "model": model or None,
        })
    data["containers"] = containers

    # Services
    services = []
    for svc in ["devforge-llm", "litellm"]:
        status = _run(["systemctl", "--user", "is-active", f"{svc}.service"])
        enabled = _run(["systemctl", "--user", "is-enabled", f"{svc}.service"])
        if not enabled or "Failed" in enabled:
            status = "not-installed"
        services.append({"name": svc, "status": status})
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
# MOTD
# ═══════════════════════════════════════════════════════════════════════

def generate_motd(structural, metrics):
    lines = []

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

    lines.append(f"{GREEN}[SYSTEM]{NC}")
    col1_w, col2_w = 24, 23
    lines.append(f"  {WHITE}{'Spec:':<10} {'OCI ARM Flex 4ocpu 24ram':<{col1_w}}   {'OS:':<5} {'Oracle Linux Server 9.7':<{col2_w}}   {'Arch:':<7} aarch64{NC}")
    mem_val = f"{mem.get('used','?')}/{mem.get('total','?')}"
    lines.append(f"  {cpu_color}{'CPU Load:':<10} {cpu_str:<{col1_w}}{NC}   {mem_color}{'Mem:':<5} {mem_val:<{col2_w}}{NC}   {'Zram:':<7} {zram if zram else 'none'}")

    # Storage — LVM
    lines.append(f"\n{GREEN}[STORAGE — LVM]{NC}")
    lines.append(f"  {BOLD}{'Device':<22} {'Size':>5} {'Used':>5} {'Avail':>5} {'Use%':>4} Mounted{NC}")
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
    ip = net.get('ip', 'N/A')
    llm_flags = next((c.get("flags", "") for c in containers if c.get("name") == "devforge-llm"), "")
    llm_port_match = re.search(r"--port\s+(\d+)", llm_flags)
    llm_port = llm_port_match.group(1) if llm_port_match else "8080"
    lines.append(f"\n{GREEN}[NETWORK]{NC}")
    lines.append(f"  IP: {ip}   SSH: 22   LLM: {llm_port}   LiteLLM: 4000   Netdata: 19999")

    # Services
    svc_parts = []
    for svc in structural.get("services", []):
        status = svc.get("status", "unknown")
        if status == "active":
            color = WHITE
        elif status == "inactive":
            color = YELLOW
        else:
            color = RED
        svc_parts.append(f"{svc['name']}: {color}{status}{NC}")
    lines.append(f"\n{GREEN}[SERVICES]{NC}")
    lines.append("  " + "   ".join(svc_parts))

    lines.append(f"{CYAN}{'='*64}{NC}")

    MOTD_FILE.write_text("\n".join(lines) + "\n")


# ═══════════════════════════════════════════════════════════════════════
# ORCHESTRATION
# ═══════════════════════════════════════════════════════════════════════

def main():
    # 1. Load previous state BEFORE overwriting (for diff)
    prev_state = load_previous_state()
    prev_structural = prev_state.get("structural") if prev_state else None

    # 2. Collect fresh data
    structural = collect_structural()
    metrics = collect_metrics()

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

    # 5. Generate MOTD
    generate_motd(structural, metrics)

    return 0


if __name__ == "__main__":
    sys.exit(main())
