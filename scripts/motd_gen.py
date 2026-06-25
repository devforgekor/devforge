#!/usr/bin/env python3
# Status: production
# Path: night_cycle.sh
"""motd_gen.py — lightweight MOTD for SSH login. Reads state.yaml + live metrics.
Runs in <1s via pam_motd.so. State is from nightly batch, metrics are live."""

import subprocess
from pathlib import Path

STATE_FILE = Path("/opt/projects/server/state.yaml")
COLORS = {"green": "\033[0;32m", "yellow": "\033[1;33m", "red": "\033[0;31m",
          "cyan": "\033[0;36m", "reset": "\033[0m", "white": "\033[0;37m"}


def _color(c: str, text: str) -> str:
    return f"{COLORS.get(c, '')}{text}{COLORS['reset']}"


def live_metrics() -> str:
    """Collect live system metrics. Must finish in <1s."""
    lines = []

    # Memory
    try:
        r = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=2)
        for ln in r.stdout.split("\n"):
            if "Mem:" in ln:
                parts = ln.split()
                used_pct = round(int(parts[2].rstrip("GiMiB")) / int(parts[1].rstrip("GiMiB")) * 100)
                c = "green" if used_pct < 70 else "yellow" if used_pct < 90 else "red"
                lines.append(_color(c, f"Memory: {parts[2]}/{parts[1]} ({used_pct}%)  "
                                    f"avail {parts[-1]}"))
            if "Swap:" in ln:
                parts = ln.split()
                lines.append(f"Swap: {parts[2]}/{parts[1]}")
    except Exception:
        pass

    # Disk
    try:
        r = subprocess.run(["df", "-h", "/", "/mnt/lv_db", "/mnt/secure_meta"],
                          capture_output=True, text=True, timeout=2)
        for ln in r.stdout.split("\n")[1:]:
            parts = ln.split()
            if len(parts) >= 5:
                pct = int(parts[4].rstrip("%"))
                c = "green" if pct < 80 else "yellow" if pct < 90 else "red"
                lines.append(_color(c, f"Disk {parts[5]}: {parts[2]}/{parts[1]} ({parts[4]})"))
    except Exception:
        pass

    # Containers
    try:
        r = subprocess.run(
            ["podman", "ps", "--format", "{{.Names}} {{.Status}}"],
            capture_output=True, text=True, timeout=3,
        )
        containers = []
        for ln in r.stdout.strip().split("\n"):
            if not ln.strip():
                continue
            name, *rest = ln.split(maxsplit=1)
            status = rest[0] if rest else ""
            c = "green" if "Up" in status and "unhealthy" not in status else "red"
            containers.append(_color(c, f"  {name}: {status[:40]}"))
        if containers:
            lines.insert(0, "Containers:")
            lines[1:1] = containers
    except Exception:
        pass

    return "\n".join(lines)


def motd() -> str:
    out = []
    out.append(_color("cyan", "=" * 64))
    out.append(_color("green", "[DevForge]") + "  ARM Oracle Linux — Podman rootless")

    # Structural info from nightly state (rarely changes)
    try:
        import yaml
        state = yaml.safe_load(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        structural = state.get("structural", {})
        net = structural.get("network", {})
        if net:
            ip = net.get("ip", "?")
            ports = net.get("ports", {})
            ssh_p = ports.get("ssh", "?")
            out.append(f"\n{_color('green', '[NETWORK]')}")
            out.append(f"IP: {ip}   SSH: {ssh_p}")
    except Exception:
        pass

    # Live metrics
    out.append(f"\n{_color('green', '[METRICS]')}")
    out.append(live_metrics())

    out.append(_color("cyan", "=" * 64))
    return "\n".join(out)


if __name__ == "__main__":
    print(motd())
