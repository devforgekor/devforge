#!/usr/bin/env python3
"""gen_motd_system.py — Generate one-line DevForge header (quick overview).
Output: /tmp/devforge-header.txt
Data: state.yaml (metrics), DB (observations count)
"""
import subprocess
import sys
from pathlib import Path

import yaml

SERVER = Path("/opt/projects/server")
STATE_FILE = SERVER / "state.yaml"
CACHE_FILE = Path("/tmp/devforge-header.txt")

# ANSI colors
RED = "\033[0;31m"
NC = "\033[0m"


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


def yesterday_observations():
    """Count observations from yesterday."""
    out = psql(
        "SELECT COUNT(*) FROM observations "
        "WHERE created_at >= CURRENT_DATE - 1 "
        "AND created_at < CURRENT_DATE"
    )
    try:
        return int(out.strip())
    except (ValueError, TypeError):
        return 0


def color_cpu(val):
    """Color load value by threshold."""
    if val >= 6:
        return f"{RED}{val:.1f}{NC}"
    elif val >= 4:
        return f"\033[0;33m{val:.1f}\033[0m"  # yellow
    return f"{val:.1f}"


def main():
    state = load_yaml(STATE_FILE)
    metrics = state.get("metrics", {})
    
    # Containers
    containers = state.get("structural", {}).get("containers", [])
    app_ctr = [c for c in containers if not c.get("name", "").startswith("pod:")]
    total_ctr = len(app_ctr)
    healthy_ctr = sum(1 for c in app_ctr if "healthy" in c.get("status", "").lower())
    unhealthy_ctr = sum(1 for c in app_ctr if "unhealthy" in c.get("status", "").lower())
    
    ctr_badge = f"{healthy_ctr}/{total_ctr}"
    if unhealthy_ctr:
        ctr_badge += f" {RED}{unhealthy_ctr}↓{NC}"
    
    # Services
    services = state.get("structural", {}).get("services", [])
    active_svc = sum(1 for s in services if s.get("status") == "active")
    
    # System metrics
    sys_m = metrics.get("system", {})
    cpu_load = sys_m.get("cpu_load", [0, 0, 0])
    cpu_cores = sys_m.get("cpu_cores", 4)
    
    mem = sys_m.get("memory", {})
    mem_used = mem.get("used", "?Gi")
    mem_total = mem.get("total", "?Gi")
    mem_pct = mem.get("percent", 0)
    
    # Memory color
    if mem_pct >= 90:
        mem_pct_str = f"{RED}{mem_pct}%{NC}"
    elif mem_pct >= 80:
        mem_pct_str = f"\033[0;33m{mem_pct}%\033[0m"
    else:
        mem_pct_str = f"{mem_pct}%"
    
    # Zram
    zram = sys_m.get("zram", "")
    zram_cycles = sys_m.get("zram_cycles", 0)
    zram_str = f" | Zram {zram} (×{zram_cycles})" if zram else ""
    
    # Observations
    obs_count = yesterday_observations()
    
    # Build one-liner
    parts = [
        "DevForge",
        f"컨테이너 {ctr_badge}",
        f"서비스 {active_svc}",
        f"CPU {color_cpu(cpu_load[0])}/{color_cpu(cpu_load[1])}/{color_cpu(cpu_load[2])}/{cpu_cores}",
        f"Mem {mem_used}/{mem_total} ({mem_pct_str}){zram_str}",
    ]
    if obs_count > 0:
        parts.append(f"Obs {obs_count}")
    
    text = " | ".join(parts)
    
    # Write or print
    if "--stdout" in sys.argv:
        print(text)
    else:
        CACHE_FILE.write_text(text + "\n")
        print(f"✓ {CACHE_FILE}", file=sys.stderr)


if __name__ == "__main__":
    main()
