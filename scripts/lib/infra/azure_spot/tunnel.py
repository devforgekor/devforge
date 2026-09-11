#!/usr/bin/env python3
# Status: experimental
# Path: imported by — lib.infra.azure_spot.__init__, orchestrator, cli
"""SSH tunnel management for Azure Spot VMs (tracked-pid based, cross-process safe)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Optional

_STATE_DIR = os.path.expanduser("~/.cache/devforge")
_STATE = os.path.join(_STATE_DIR, "spot_tunnels.json")


def _load_state() -> dict:
    try:
        with open(_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        with open(_STATE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _pid_is_ssh(pid: int) -> bool:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True)
    return r.returncode == 0 and "ssh" in (r.stdout or "")


def open_spot_tunnel(
    label: str, ip: str, remote_port: int, local_port: int,
    ssh_user: str = "azureuser",
) -> Optional[subprocess.Popen]:
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-N", "-L", f"{local_port}:localhost:{remote_port}",
        f"{ssh_user}@{ip}",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        if proc.poll() is not None:
            print(f"  Tunnel for {label} failed to start (exit={proc.returncode})")
            return None
        state = _load_state()
        state[str(local_port)] = {"pid": proc.pid, "ip": ip, "label": label, "remote": remote_port}
        _save_state(state)
        print(f"  Tunnel for {label} localhost:{local_port} → {ip}:{remote_port} (pid={proc.pid})")
        return proc
    except Exception as e:
        print(f"  Tunnel for {label} error: {e}")
        return None


def close_spot_tunnel(local_port: int) -> None:
    """Kill ONLY the tracked ssh tunnel on local_port (never unrelated processes)."""
    state = _load_state()
    info = state.pop(str(local_port), None)
    _save_state(state)
    if not info:
        return
    pid = info.get("pid")
    if pid and _pid_is_ssh(pid):
        subprocess.run(["kill", str(pid)], timeout=5)
        print(f"  Closed tunnel pid={pid} on port {local_port}")


def close_spot_tunnels(base_port: int, count: int = 8) -> list:
    """Sweep tracked tunnels in [base_port, base_port+count)."""
    closed = []
    for port in range(base_port, base_port + count):
        before = set(_load_state().keys())
        close_spot_tunnel(port)
        if str(port) in before:
            closed.append(port)
    return closed
