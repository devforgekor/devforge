#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/exp_runner.py, day_runner.py, night_runner.py
"""Container lifecycle management — stop, start, health, memory reclaim."""

import os, subprocess, time, urllib.request

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def log(msg):
    from datetime import datetime, timezone
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)

def stop_all():
    """Stop both LLM containers."""
    for svc in ["container-devforge-pod-b.service", "container-devforge-pod-a.service"]:
        subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10)

def free_memory(level=1):
    """Memory reclamation. level:
    1 = sync + 15s wait (default)
    2 = level1 + drop_caches + swap reactivation
    3 = level2 + 30s wait + oom_score_adj propagation
    """
    os.sync()
    log(f"  Memory reclaim level {level}: synced, waiting...")

    if level >= 2:
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
            log("  drop_caches=3 OK")
        except Exception as e:
            log(f"  drop_caches failed (non-fatal): {e}")

        try:
            subprocess.run(["swapoff", "-a"], capture_output=True, timeout=30)
            subprocess.run(["swapon", "-a"], capture_output=True, timeout=30)
            log("  swap re-activated OK")
        except Exception as e:
            log(f"  swap reactivate failed (non-fatal): {e}")

    wait_time = 30 if level >= 3 else 15
    for i in range(wait_time):
        if i % 5 == 0:
            report_memory(f"reclaim ({i}s)")
        time.sleep(1)

    report_memory("after reclaim")

def report_memory(label=""):
    """Log free/available memory."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    kb = int(line.split()[1])
                    mb = kb // 1024
                    log(f"  MemAvailable: {mb}MB {label}")
                    return
        log(f"  MemAvailable: ? {label}")
    except Exception:
        pass

def get_available_mb():
    """Return MemAvailable in MB, or 0 on error."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return 0

def write_mode(pod, mode):
    """Write mode file atomically."""
    if pod == "pod-b":
        from lib.pod_manager import _write_mode_env
        mode_port = {"day": 8082, "embed": 8081, "verify": 8084}
        port = mode_port.get(mode, 8082)
        _write_mode_env(mode, port)
        return
    path = f"/opt/ai_data/scripts/current-mode-{pod}.env"
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(f"MODE={mode}")
    os.rename(tmp, path)

def start_pod_a(timeout=120):
    """Start Pod A (reserved:8080)."""
    log("  Starting Pod A (reserved:8080)...")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=60)
    return wait_health(8080, timeout)

def start_pod_b(timeout=120):
    """Start Pod B (extractor:8082)."""
    log("  Starting Pod B (extractor:8082)...")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b.service"],
                   capture_output=True, timeout=60)
    return wait_health(8082, timeout)

def recover_and_restart(attempt=1):
    """OOM/failure recovery + day mode restart.

    attempt=1: default — stop + sync + 15s + extract(7B)+verify(14B)
    attempt=2: aggressive — drop_caches + swap off/on + extract(7B)+verify(14B)
    attempt=3: last resort — minimal mode (extract only, no verify/review)
    """
    stop_all()
    free_memory(level=min(attempt, 3))
    write_mode("pod-b", "day")
    write_mode("pod-a", "day")

    if attempt <= 2:
        log("  Attempt: Pod A reserved + Pod B extractor (standard day mode)")
        pod_a_ready = start_pod_a(120)
        log(f"  Pod A (reserved:8080) = {'OK' if pod_a_ready else 'TIMEOUT'}")

        if not pod_a_ready and attempt == 2:
            report_memory("after Pod A failure")
            stop_all()
            free_memory(level=2)
            pod_a_ready = start_pod_a(120)
            log(f"  Pod A retry (reserved:8080) = {'OK' if pod_a_ready else 'TIMEOUT'}")

        if pod_a_ready:
            pod_b_ready = start_pod_b(180)
            log(f"  Pod B (extractor:8082) = {'OK' if pod_b_ready else 'TIMEOUT'}")
            if pod_b_ready:
                return True

        log("  Escalating to minimal mode (day model only)...")

    stop_all()
    free_memory(level=3)
    write_mode("pod-b", "day")

    log("  Minimal mode: Pod B only (extractor:8082)")
    pod_b_ready = start_pod_b(300)
    if pod_b_ready:
        log("  Minimal mode OK: day model running alone")
        return True

    log("  FATAL: even minimal mode failed")
    return False
