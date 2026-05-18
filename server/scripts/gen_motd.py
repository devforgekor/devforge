#!/usr/bin/env python3
"""gen_motd.py — Generate detailed DevForge system status with per-core CPU + zram cycles.
Output: /tmp/devforge-motd.txt
Data: state.yaml + DB + systemd + network info
"""
import subprocess
import sys
from pathlib import Path

import yaml

SERVER = Path("/opt/projects/server")
STATE_FILE = SERVER / "state.yaml"
HANDOVER_FILE = SERVER / "handover.yaml"
CACHE_FILE = Path("/tmp/devforge-motd.txt")

# ANSI colors
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
GREEN = "\033[0;32m"
NC = "\033[0m"
BOLD = "\033[1m"


def load_yaml(path: Path):
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def psql(query: str, timeout=5):
    """Execute PostgreSQL query via podman."""
    try:
        r = subprocess.run(
            ["podman", "exec", "postgres", "psql", "-U", "devforge",
             "-d", "devforge_app", "--no-align", "--tuples-only", "-c", query],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def get_per_core_cpu_load():
    """Get per-CPU usage from /proc/stat."""
    try:
        loads = []
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu") and not line.startswith("cpu "):
                    parts = line.split()
                    if len(parts) >= 8:
                        user = int(parts[1])
                        nice = int(parts[2])
                        system = int(parts[3])
                        idle = int(parts[4])
                        total = user + nice + system + idle
                        if total > 0:
                            pct = (user + nice + system) * 100 // total
                            loads.append(pct)
        return loads
    except Exception:
        return []


def get_network_info():
    """Get network interfaces."""
    try:
        r = subprocess.run(
            ["ip", "-br", "addr"],
            capture_output=True, text=True, timeout=5
        )
        lines = []
        for line in r.stdout.strip().splitlines():
            if line.strip() and not line.startswith("lo "):
                parts = line.split()
                if len(parts) >= 3:
                    name = parts[0]
                    status = parts[1]
                    color = GREEN if status == "UP" else RED if status == "DOWN" else YELLOW
                    addr = parts[2] if len(parts) > 2 else "?"
                    lines.append((name, f"{color}{status}{NC}", addr))
        return lines
    except Exception:
        return []


def get_uptime():
    """Get system uptime."""
    try:
        with open("/proc/uptime") as f:
            uptime_sec = float(f.read().split()[0])
        days = int(uptime_sec // 86400)
        hours = int((uptime_sec % 86400) // 3600)
        return f"{days}d {hours}h"
    except Exception:
        return "?"


def main():
    state = load_yaml(STATE_FILE)
    handover = load_yaml(HANDOVER_FILE)
    metrics = state.get("metrics", {})
    structural = state.get("structural", {})
    
    lines = []
    
    # ─── SYSTEM METRICS ────────────────────────────────────────────
    lines.append(f"{BOLD}[SYSTEM]{NC}")
    
    sys_m = metrics.get("system", {})
    cpu_load = sys_m.get("cpu_load", [0, 0, 0])
    cpu_cores = sys_m.get("cpu_cores", 4)
    
    mem = sys_m.get("memory", {})
    mem_used = mem.get("used", "?Gi")
    mem_total = mem.get("total", "?Gi")
    mem_pct = mem.get("percent", 0)
    
    zram = sys_m.get("zram", "")
    zram_cycles = sys_m.get("zram_cycles", 0)
    
    uptime = get_uptime()
    per_core = get_per_core_cpu_load()
    
    # CPU detail with 15m load
    if cpu_load[2] >= 4:
        load_str = f"{RED}{cpu_load[2]:.1f}{NC}"
    elif cpu_load[2] >= 2:
        load_str = f"{YELLOW}{cpu_load[2]:.1f}{NC}"
    else:
        load_str = f"{cpu_load[2]:.1f}"
    lines.append(f"  CPU Cores: {cpu_cores} | Load (15m): {load_str}")
    lines.append(f"    (1m: {cpu_load[0]:.1f}, 5m: {cpu_load[1]:.1f}, 15m: {cpu_load[2]:.1f})")
    
    # Per-core CPU usage
    if per_core:
        core_str_parts = []
        for i, pct in enumerate(per_core):
            if pct >= 80:
                core_str_parts.append(f"{RED}{i}:{pct}%{NC}")
            elif pct >= 50:
                core_str_parts.append(f"{YELLOW}{i}:{pct}%{NC}")
            else:
                core_str_parts.append(f"{i}:{pct}%")
        lines.append(f"    Cores: {' '.join(core_str_parts)}")
    
    # Memory detail
    if mem_pct >= 90:
        mem_str = f"{RED}{mem_used}/{mem_total} ({mem_pct}%){NC}"
    elif mem_pct >= 80:
        mem_str = f"{YELLOW}{mem_used}/{mem_total} ({mem_pct}%){NC}"
    else:
        mem_str = f"{mem_used}/{mem_total} ({mem_pct}%)"
    lines.append(f"  Memory: {mem_str}")
    
    # Zram detail
    if zram:
        lines.append(f"  Zram: {zram} (cycles: {zram_cycles})")
    
    # Uptime
    lines.append(f"  Uptime: {uptime}")
    
    # ─── NETWORK ───────────────────────────────────────────────────
    network = get_network_info()
    if network:
        lines.append(f"\n{BOLD}[NETWORK]{NC}")
        for name, status, addr in network:
            lines.append(f"  {name:15s} {status:15s} {addr}")
    
    # ─── CONTAINERS ────────────────────────────────────────────────
    containers = structural.get("containers", [])
    app_ctr = [c for c in containers if not c.get("name", "").startswith("pod:")]
    if app_ctr:
        lines.append(f"\n{BOLD}[CONTAINERS]{NC}")
        healthy = sum(1 for c in app_ctr if "healthy" in c.get("status", "").lower())
        unhealthy = [c for c in app_ctr if "unhealthy" in c.get("status", "").lower()]
        
        if unhealthy:
            lines.append(f"  Status: {GREEN}{healthy}{NC}/{len(app_ctr)} {RED}[{len(unhealthy)} unhealthy]{NC}")
            for c in unhealthy:
                uptime_c = c.get("uptime", "?")
                health = c.get("health", "unhealthy")
                lines.append(f"      x {RED}{c['name']:25s}{NC} {health:15s} ({uptime_c})")
        else:
            lines.append(f"  Status: {healthy}/{len(app_ctr)}")
        
        for c in app_ctr:
            status = c.get("status", "?")
            uptime_c = c.get("uptime", "?")
            if "unhealthy" not in status.lower():
                lines.append(f"      o {c['name']:25s} {uptime_c:15s}")
    
    # ─── SERVICES ──────────────────────────────────────────────────
    services = structural.get("services", [])
    if services:
        lines.append(f"\n{BOLD}[SERVICES]{NC}")
        active = [s for s in services if s.get("status") == "active"]
        failed = [s for s in services if s.get("status") not in ("active", "inactive")]
        
        if failed:
            lines.append(f"  Status: {GREEN}{len(active)}{NC}/{len(services)} {RED}[{len(failed)} failed]{NC}")
            for s in failed:
                lines.append(f"      x {RED}{s['name']}{NC}: {s.get('status', '?')}")
        else:
            lines.append(f"  Status: {len(active)}/{len(services)}")
        
        for s in active:
            lines.append(f"      o {s['name']}")
    
    # ─── STORAGE ───────────────────────────────────────────────────
    storage = metrics.get("storage_use", [])
    if storage:
        lines.append(f"\n{BOLD}[STORAGE]{NC}")
        for s in storage:
            device = s.get("device", "?")
            mount = s.get("mount", "?")
            used = s.get("used", "?")
            total = s.get("size", "?")
            pct = s.get("use_pct", 0)
            
            if pct >= 90:
                device_str = f"{RED}{device:20s} {mount:20s} {used:6s}/{total:6s} {pct:3d}%{NC}"
            elif pct >= 80:
                device_str = f"{YELLOW}{device:20s} {mount:20s} {used:6s}/{total:6s} {pct:3d}%{NC}"
            else:
                device_str = f"{device:20s} {mount:20s} {used:6s}/{total:6s} {pct:3d}%"
            
            lines.append(f"  {device_str}")
    
    # ─── KNOWN ISSUES ────────────────────────────────────────────
    known_issues = handover.get("known_issues", {})
    if known_issues:
        lines.append(f"\n{BOLD}[알려진 문제]{NC}")
        for key, value in list(known_issues.items())[:5]:
            desc = value if isinstance(value, str) else value.get("description", str(value))[:60]
            lines.append(f"  • {key}: {desc}")
        if len(known_issues) > 5:
            lines.append(f"  ... +{len(known_issues) - 5} more")
    
    # ─── DECISIONS ────────────────────────────────────────────────
    decisions = handover.get("decisions", {})
    if decisions:
        lines.append(f"\n{BOLD}[최근 결정]{NC}")
        for key, value in list(decisions.items())[:5]:
            desc = value if isinstance(value, str) else value.get("description", str(value))[:60]
            lines.append(f"  • {key}: {desc}")
    
    text = "\n".join(lines)
    
    if "--stdout" in sys.argv:
        print(text)
    else:
        CACHE_FILE.write_text(text + "\n")
        print(f"✓ {CACHE_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
