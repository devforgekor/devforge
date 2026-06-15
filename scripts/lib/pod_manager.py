#!/usr/bin/env python3
# Status: production
# Path: imported by — pipelines/prj_cycle.py, night_cycle.py, night_runner.py, day_runner.py, day_cycle.sh
"""Container management for DevForge — Pod A (devforge-pod-a :8080) and Pod B (devforge-pod-b :8081-8089).

Port map:
  8080  Pod A  — Reserved for operator (future)
  8081  Pod B  — embed(f16 day) / proposer(30B night)
  8082  Pod B  — extract(7B day) / reflector(14B night)
  8083  Pod B  — verify(14B day) / judge(N14B night)
  8084  Pod B  — verifier(27B)
  8085+ Pod B  — Future / Azure SSH tunnels
"""

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
EMBEDDING_PROTECT_FILE = '/opt/ai_data/scripts/EMBEDDING_IN_PROGRESS'
TIMEOUT = 7200

MODEL_METADATA = {
    # Pod A — reserved for operator (future use)
    "reviewer":   {"file": "Qwen2.5-Coder-7B-Instruct-Q8_0.gguf",  "size": "7.6GB", "port": 8080, "mode": "reserved"},
    # Pod B models — port assigned per mode (not from env file):
    #   8081: embed(f16 day) / proposer(30B night)
    #   8082: extract(7B day) / reflector(14B night)
    #   8083: verify(14B day) / judge(N14B night)
    #   8084: verifier(27B)
    "embed":      {
        "file": "Qwen3-Embedding-8B-f16.gguf",
        "size": "16.3GB", "port": 8081, "mode": "embed",
        "model_name": "embed", "ctx": 16384,
        "threads": 4, "threads_batch": 4,
        "parallel": 1,
    },
    "proposer":   {
        "file": "Qwen3-Coder-30B-A3B-Instruct-Q4_K_M.gguf",
        "size": "17GB", "port": 8081, "mode": "review-p",
        "model_name": "proposer", "ctx": 8192, "cache_ram": 1024, "mlock": 0,
        "evict_room": 18000, "memory_check": 18000, "memory_check_mode": "fatal",
        "report_memory": "1", "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attn": "1",
    },
    "extractor":  {
        "file": "Qwen2.5-Coder-7B-Instruct-Q8_0.gguf",
        "size": "7.6GB", "port": 8082, "mode": "day",
        "model_name": "extractor", "ctx": 8192, "cache_ram": 1024, "evict_room": 8000,
        "threads": 4, "threads_batch": 4,
        "parallel": 3, "ubatch_size": 512,
    },
    "reflector":  {
        "file": "Qwen2.5-Coder-14B-Instruct-Q8_0.gguf",
        "size": "15.7GB","port": 8082, "mode": "review-r",
        "model_name": "reflector", "ctx": 8192, "cache_ram": 1024,
        "evict_room": 16000, "memory_check": 16000, "memory_check_mode": "fatal",
    },
    "judge":      {
        "file": "NextCoder-14B-Q8_0.gguf",
        "size": "15.0GB", "port": 8083, "mode": "review-j",
        "model_name": "judge", "ctx": 6144, "cache_ram": 512, "mlock": 0,
        "evict_room": 16000, "memory_check": 5000, "memory_check_mode": "warn", "report_memory": "1",
        "parallel": 3, "ubatch_size": 512,
    },
    "verifier":   {
        "file": "Qwen3.6-27B.i1-IQ4_XS.gguf",
        "size": "13.7GB", "port": 8084, "mode": "verify",
        "model_name": "verifier-iq4xs", "ctx": 6144, "cache_ram": 1024, "mlock": 0,
        "evict_room": 10000, "memory_check": 8000, "memory_check_mode": "warn",
        "report_memory": "1", "cache_type_k": "q8_0", "cache_type_v": "q8_0", "flash_attn": "1",
    },
    "test_14b_q8": {
        "file": "NextCoder-14B-Q8_0.gguf",
        "size": "15.7GB", "port": 8083, "mode": "test-q8",
        "model_name": "test-14b-q8", "ctx": 8192, "cache_ram": 1024,
        "evict_room": 16000, "memory_check": 16000, "memory_check_mode": "warn",
    },
    "test_nextcoder_q8": {
        "file": "NextCoder-14B-Q8_0.gguf",
        "size": "15.0GB", "port": 8083, "mode": "test-q8",
        "model_name": "test-nextcoder-q8", "ctx": 8192, "cache_ram": 1024,
        "evict_room": 16000, "memory_check": 16000, "memory_check_mode": "warn",
    },
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
    """Advanced memory reclamation via container_manager.free_memory."""
    try:
        from lib.infra.container_manager import free_memory
        free_memory(level=2)
    except ImportError:
        import os as _os
        _os.sync()
        log("  Memory reclaim (fallback): synced fs, waiting 15s...")
        time.sleep(15)


def _container_service_name(port):
    """Map port to systemd service name."""
    if port == 8080:
        return "container-devforge-pod-a"
    return "container-devforge-pod-b"


def _check_container_health(port, label):
    """Check container-level health beyond HTTP /health.

    Inspects:
      - systemd NRestarts (> 0 = crashloop detected)
      - podman container uptime (restarted recently -> warn)
      - entrypoint file existence on host
      - /slots: detect stuck processing (slot busy > 30s without being stuck)

    Returns tuple (healthy: bool, warnings: list[str]).
    """
    warnings = []
    svc = _container_service_name(port)

    # 1. systemd restart count
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", f"{svc}.service", "-p", "NRestarts", "--value"],
            capture_output=True, text=True, timeout=10)
        restarts = int(r.stdout.strip())
        if restarts > 0:
            warnings.append(f"{svc} NRestarts={restarts} — possible crashloop")
    except Exception:
        pass

    # 2. podman container status (uptime / restart count)
    podman_name = "devforge-pod-a" if "pod-a" in svc else "devforge-pod-b"
    try:
        r = subprocess.run(
            ["podman", "ps", "--filter", f"name={podman_name}", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=10)
        status = r.stdout.strip()
        if not status:
            warnings.append(f"{svc} not in podman ps — container may be dead")
        elif status.startswith("Up ") and "second" in status:
            warnings.append(f"{svc} just started ({status}) — may not be fully initialized")
        elif "unhealthy" in status:
            warnings.append(f"{svc} status=unhealthy — health check failing")
    except Exception:
        pass

    # 3. entrypoint file existence (preflight)
    entrypoint_map = {
        "container-devforge-pod-a": "/opt/ai_data/scripts/reviewer-entrypoint.sh",
        "container-devforge-pod-b": "/opt/ai_data/scripts/pod-b-entrypoint.sh",
    }
    ep_path = entrypoint_map.get(svc)
    if ep_path and not os.path.exists(ep_path):
        warnings.append(f"entrypoint missing: {ep_path}")

    for w in warnings:
        log(f"  [container-warn] {w}")
    return len(warnings) == 0, warnings


# Models requiring night-mode isolation (heavy, background timer conflict -> OOM risk)
NIGHT_MODELS = frozenset({"proposer", "reflector", "judge", "verifier"})


def _is_embedding_protected() -> bool:
    """Check if embedding backfill is currently protected."""
    return os.path.exists(EMBEDDING_PROTECT_FILE)


def _kill_stray_pasta(ports):
    for port in ports:
        if str(port) == '8081' and _is_embedding_protected():
            log("  [PROTECT] skipping stray kill for :8081 (embedding in progress)")
            continue
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
        if _is_embedding_protected():
            log("  [PROTECT] skipping Pod B stop (night mode) - embedding protected")
        else:
            subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b.service"],
                           capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b.service"],
                           capture_output=True, timeout=10)
        # Night: stop background services that could trigger OOM with heavy models
        for svc in ("devforge-day-cycle.service", "devforge-day-cycle.timer",
           "devforge-night-cycle.service",
                     "devforge-night-cycle.timer"):
            subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10)
        _kill_stray_pasta(("8081", "8082", "8083", "8084"))
    else:
        if _is_embedding_protected():
            log("  [PROTECT] Pod B stop bypassed (day mode) - embedding protected")
        else:
            subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b.service"],
                           capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b.service"],
                           capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                       capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                       capture_output=True, timeout=10)
        _kill_stray_pasta(("8080", "8081", "8082", "8083", "8084"))
    _reclaim_memory()


def _extract_pasta_pid(ss_out: str, port: str) -> int | None:
    """Extract pasta PID from ``ss -tlnp`` output for *port*."""
    for line in ss_out.splitlines():
        if f":{port}" in line and "pasta" in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def _write_mode_env(mode: str, port: int) -> None:
    """Write Pod B env file — SSOT is MODEL_METADATA.

    Entrypoint reads this file at startup instead of hardcoding model config.
    Looks up by (mode field or dict key, port) which uniquely identifies each Pod B model.
    """
    meta = None
    for v in MODEL_METADATA.values():
        if v.get("port") == port and (v.get("mode") == mode or v.get("model_name") == mode):
            meta = v
            break
    if meta is None:
        # Fallback: lookup by dict key
        meta = MODEL_METADATA.get(mode)
        if meta and meta.get("port") != port:
            meta = None

    entrypoint_mode = meta["mode"] if meta else mode
    pairs = [("MODE", entrypoint_mode)]
    if meta:
        f = meta.get
        pairs += [
            ("MODEL_NAME", f("model_name", mode)),
            ("PORT", str(port)),
            ("MODEL_FILE", meta["file"]),
            ("CTX_SIZE", str(f("ctx", 8192))),
            ("THREADS", str(f("threads", 4))),
            ("THREADS_BATCH", str(f("threads_batch", 4))),
        ]
        for key, env_key in [
            ("cache_ram", "CACHE_RAM"),
            ("mlock", "MLOCK"),
            ("evict_room", "EVICT_ROOM"),
            ("memory_check", "MEMORY_CHECK"),
            ("memory_check_mode", "MEMORY_CHECK_MODE"),
            ("report_memory", "REPORT_MEMORY"),
            ("cache_type_k", "CACHE_TYPE_K"),
            ("cache_type_v", "CACHE_TYPE_V"),
            ("flash_attn", "FLASH_ATTN"),
            ("batch_size", "BATCH_SIZE"),
            ("ubatch_size", "UBATCH_SIZE"),
            ("parallel", "PARALLEL"),
        ]:
            val = f(key)
            if val is not None and val != "":
                pairs.append((env_key, str(val)))

    lines = [f"{k}={v}" for k, v in pairs]
    with open(MODE_FILE_B, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"  wrote env for {mode}:{port} ({meta['file'] if meta else '?'})")


def start_pod_a_only(mode, port, dry_run=False):
    """Start Pod A only, Pod B untouched. For operator mode."""
    log(f"  POD A -> {mode} (:{port}) — Pod B kept running")
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=10)
    _kill_stray_pasta(("8080",))
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port)
    if ok:
        log(f"  :{port} health OK")
        ok = wait_probe(port, mode, timeout=600)
        _check_container_health(port, mode)
    else:
        log(f"  :{port} TIMEOUT (Pod A {mode})")
    return ok


def stop_pod_a(dry_run=False):
    """Stop Pod A only, Pod B untouched."""
    log("  Pod A stop (Pod B running)...")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    _kill_stray_pasta(("8080",))


def start_pod_b(mode, port, night=False, dry_run=False, skip_probe=False):
    log(f"  POD B -> {mode} (:{port})")
    _write_mode_env(mode, port)
    kill_all(night=night, dry_run=dry_run)
    ports_to_clean = ("8081", "8082", "8083", "8084") if night else (str(port),)
    _kill_stray_pasta(ports_to_clean)
    health_timeout = 1200 if night else 600
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port, timeout=health_timeout)
    if ok:
        log(f"  :{port} health OK")
        if not skip_probe:
            ok = wait_probe(port, mode, timeout=600)
    if ok:
        log(f"  :{port} ready")
        _check_container_health(port, mode)
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
        _check_container_health(port, mode)
    else:
        log(f"  :{port} TIMEOUT (Pod A {mode})")
    return ok


def start_day_both(dry_run=False):
    """DEPRECATED — Pod A is reserved operator, no longer runs day reviewer."""
    log("  start_day_both: DEPRECATED — Pod A is reserved operator port")
    return True


def ensure_model(physical_name, skip_if_healthy=False, dry_run=False):
    if dry_run:
        log(f"  [DRY] ensure_model({physical_name}) -> OK (mock)")
        return True
    meta = MODEL_METADATA.get(physical_name)
    if not meta:
        log(f"  Unknown model: {physical_name}")
        return False
    night = physical_name in NIGHT_MODELS
    if skip_if_healthy:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{meta['port']}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    log(f"  :{meta['port']} already healthy — skip restart")
                    _check_container_health(meta['port'], physical_name)
                    return True
        except Exception:
            pass
    if meta["port"] == 8080:
        ok = start_pod_a(meta["mode"], meta["port"], dry_run=dry_run)
    else:
        ok = start_pod_b(meta["mode"], meta["port"], night=night, dry_run=dry_run)
    if ok:
        return True
    log(f"  ensure_model({physical_name}) failed — retrying after GC + 10s")
    _reclaim_memory()
    import gc
    gc.collect()
    time.sleep(10)
    if meta["port"] == 8080:
        return start_pod_a(meta["mode"], meta["port"], dry_run=dry_run)
    return start_pod_b(meta["mode"], meta["port"], night=night, dry_run=dry_run)
