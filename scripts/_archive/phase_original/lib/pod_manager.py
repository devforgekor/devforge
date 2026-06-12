#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/prj_cycle.py
"""Container management for P-R-J pipeline — Pod A (devforge-pod-a) and Pod B (devforge-pod-b)."""

from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import time
import urllib.request
from datetime import datetime, timezone


MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
TIMEOUT = 7200

MODEL_METADATA = {
    "extractor":  {"file": "Qwen2.5-Coder-3B-Instruct.Q8_0.gguf",  "size": "3.1GB", "port": 8082, "mode": "day"},
    "reviewer":   {"file": "Qwen2.5-Coder-7B-Instruct.Q8_0.gguf",  "size": "7.6GB", "port": 8080, "mode": "day"},
    "reflector":  {"file": "Qwen2.5-Coder-14B-Instruct.Q8_0.gguf", "size": "15.7GB","port": 8080, "mode": "review-r"},
    "proposer":   {"file": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_S.gguf","size":"17GB","port":8080, "mode":"review-p"},
    "judge":      {"file":"NextCoder-14B-Q8_0.gguf",               "size":"15.7GB","port":8080, "mode":"review-j"},
    "verifier":   {"file": "Qwen3.6-27B-Q4_K_M.gguf",              "size": "16GB", "port": 8081, "mode": "verify"},
}


def model_info(key):
    m = MODEL_METADATA.get(key, {})
    return f"{key}({m.get('file','?')} {m.get('size','?')} :{m.get('port','?')})"


def _ts():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}", flush=True)


def wait_health(port, timeout=600):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def wait_probe(port, model_name, timeout=300):
    t0 = time.monotonic()
    body = json.dumps({
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5, "temperature": 0.1, "stream": False,
    }).encode()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                if data.get("choices") and data["choices"][0].get("message"):
                    log(f"  probe OK ({model_name})")
                    return True
        except Exception:
            pass
        time.sleep(5)
    log(f"  probe TIMEOUT ({model_name})")
    return False


def _reclaim_memory():
    import os as _os
    _os.sync()
    log("  Memory reclaim: synced fs, waiting 15s for kernel reclaim...")
    time.sleep(15)
    log("  Memory reclaim: done")


# Models requiring night-mode isolation (heavy, background timer conflict → OOM risk)
NIGHT_MODELS = frozenset({"proposer", "reflector", "judge", "verifier"})


def _kill_stray_pasta(ports):
    for port in ports:
        try:
            r = subprocess.run(
                ["ss", "-tlnp", f"sport = :{port}"],
                capture_output=True, text=True, timeout=10)
            if "pasta" in r.stdout:
                pid = _extract_pasta_pid(r.stdout, port)
                if pid:
                    log(f"  killing stray pasta (PID {pid}) holding :{port}")
                    os.kill(pid, signal.SIGKILL)
        except Exception:
            pass


def kill_all(night=False, dry_run=False):
    if dry_run:
        log("  [DRY] kill_all() skipped")
        return
    if night:
        log("  systemctl stop (night mode — Pod B only)...")
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=10)
        # Night: stop background services that could trigger OOM with heavy models
        # systemd Restart=on-failure would restart containers; reset-failed prevents it
        for svc in ("devforge-15m-cycle.service", "devforge-15m-cycle.timer",
                     "devforge-watchdog.service", "devforge-nightly.service",
                     "devforge-nightly.timer"):
            subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10)
        _kill_stray_pasta(("8080", "8081"))
    else:
        log("  systemctl stop containers...")
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                       capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                       capture_output=True, timeout=10)
        _kill_stray_pasta(("8080", "8081", "8082"))
    _reclaim_memory()


def _extract_pasta_pid(ss_out: str, port: str) -> int | None:
    """Extract pasta PID from ``ss -tlnp`` output for *port*."""
    for line in ss_out.splitlines():
        if f":{port}" in line and "pasta" in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def start_pod_a_only(mode, port, dry_run=False):
    """Start Pod A only, Pod B untouched. For day-mode fast switching (extract ↔ verify)."""
    log(f"  POD A -> {mode} (:{port}) — Pod B kept running")
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=10)
    _kill_stray_pasta(("8082",))
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port)
    if ok:
        log(f"  :{port} health OK")
        ok = wait_probe(port, mode, timeout=600)
    else:
        log(f"  :{port} TIMEOUT (Pod A {mode})")
    return ok


def stop_pod_a(dry_run=False):
    """Stop Pod A only, Pod B untouched. No reclaim (Pod B still needs RAM)."""
    log("  Pod A stop (Pod B running)...")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=10)
    _kill_stray_pasta(("8082",))


def start_pod_b(mode, port, night=False, skip_if_healthy=False, dry_run=False):
    log(f"  POD B -> {mode} (:{port})")
    if skip_if_healthy:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    log(f"  :{port} already healthy — keeping running")
                    return True
        except Exception:
            pass
        log(f"  :{port} not healthy, starting...")
    with open(MODE_FILE_B, "w") as f:
        f.write(f"MODE={mode}")
    kill_all(night=night, dry_run=dry_run)
    # Stray pasta from failed restart cycle must be killed BEFORE starting,
    # otherwise systemctl start will fail immediately (port conflict)
    ports_to_clean = ("8080", "8081") if night else (str(port),)
    _kill_stray_pasta(ports_to_clean)
    # Night models (30B/14B) need more time: systemd Restart=on-failure may
    # restart once (OOM during repack), adding ~60s to total load time
    health_timeout = 1200 if night else 600
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port, timeout=health_timeout)
    if ok:
        log(f"  :{port} health OK")
        ok = wait_probe(port, mode, timeout=600)
    if ok:
        log(f"  :{port} ready")
        time.sleep(5)
    return ok


def start_pod_a(mode, port, dry_run=False):
    log(f"  POD A -> {mode} (:{port})")
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
    kill_all(dry_run=dry_run)
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port)
    if ok:
        log(f"  :{port} health OK")
        ok = wait_probe(port, mode, timeout=600)
    else:
        log(f"  :{port} TIMEOUT (Pod A {mode})")
    return ok


def start_day_both(dry_run=False):
    log("  DAY MODE: Pod A (day_r) -> Pod B (day_p/day_j/day_mcp, 순차)")
    with open(MODE_FILE_B, "w") as f:
        f.write("MODE=day")
    with open(MODE_FILE_A, "w") as f:
        f.write("MODE=day")
    kill_all(dry_run=dry_run)
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=60)
    pod_a_ready = wait_health(8082)
    if pod_a_ready:
        log(f"  :8082 ready (Pod A day_r)")
    else:
        log(f"  :8082 TIMEOUT (Pod A day_r)")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b.service"],
                   capture_output=True, timeout=60)
    pod_b_ready = wait_health(8080)
    if pod_b_ready:
        log(f"  :8080 ready (Pod B day)")
    else:
        log(f"  :8080 TIMEOUT (Pod B day)")
    return pod_a_ready and pod_b_ready


def ensure_model(physical_name, skip_if_healthy=False, dry_run=False):
    if dry_run:
        log(f"  [DRY] ensure_model({physical_name}) → OK (mock)")
        return True
    meta = MODEL_METADATA.get(physical_name)
    if not meta:
        log(f"  Unknown model: {physical_name}")
        return False
    # Route to correct pod: extractor = Pod A (:8082), others = Pod B
    night = physical_name in NIGHT_MODELS
    if meta["port"] == 8082:
        return start_pod_a(meta["mode"], meta["port"], dry_run=dry_run)
    return start_pod_b(meta["mode"], meta["port"], night=night,
                       skip_if_healthy=skip_if_healthy, dry_run=dry_run)
