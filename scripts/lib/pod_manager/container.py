#!/usr/bin/env python3
# Status: production
"""Container health checks, model fingerprint, pasta management, env writing."""

from __future__ import annotations
import json
import os
import re
import signal
import subprocess
import time
import urllib.request

from lib.pod_manager.models import MODEL_METADATA


def _ts():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg):
    print(f"[{_ts()}] {msg}", flush=True)


def _reclaim_memory():
    try:
        from lib.infra.container_manager import free_memory
        free_memory(level=2)
    except ImportError:
        os.sync()
        log("  Memory reclaim (fallback): synced fs, waiting 15s...")
        time.sleep(15)


def _container_service_name(port):
    if port == 8080:
        return "container-devforge-pod-a"
    return "devforge-pod-b"


def _check_container_health(port, label):
    warnings = []
    svc = _container_service_name(port)

    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", f"{svc}.service", "-p", "NRestarts", "--value"],
            capture_output=True, text=True, timeout=10)
        restarts = int(r.stdout.strip())
        if restarts > 0:
            warnings.append(f"{svc} NRestarts={restarts} — possible crashloop")
    except Exception:
        pass

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


def _get_model_fingerprint(port):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models")
        with urllib.request.urlopen(req, timeout=3) as r:
            data = json.loads(r.read())
            models = data.get("models", [])
            if models:
                path = models[0].get("model", "") or models[0].get("name", "")
                if path:
                    return os.path.basename(path)
    except Exception:
        pass
    return None


def _check_model_identity(port, model_key):
    expected_file = MODEL_METADATA.get(model_key, {}).get("file")
    if not expected_file:
        return True
    actual_file = _get_model_fingerprint(port)
    if not actual_file:
        return False
    ok = actual_file == expected_file
    if not ok:
        log(f"  [model-id] :{port} has {actual_file}, expected {expected_file}")
    return ok


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


def _extract_pasta_pid(ss_out: str, port: str) -> int | None:
    for line in ss_out.splitlines():
        if f":{port}" in line and "pasta" in line:
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    return None


def _write_mode_env(mode: str, port: int, model_key: str | None = None) -> None:
    MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
    meta = None
    if model_key:
        meta = MODEL_METADATA.get(model_key)
    if meta is None:
        for v in MODEL_METADATA.values():
            if v.get("port") == port and (v.get("mode") == mode or v.get("model_name") == mode):
                meta = v
                break
    if meta is None:
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
            ("cpus", "CPUS"),
        ]:
            val = f(key)
            if val is not None and val != "":
                pairs.append((env_key, str(val)))

    lines = [f"{k}={v}" for k, v in pairs]
    with open(MODE_FILE_B, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    log(f"  wrote env for {mode}:{port} ({meta['file'] if meta else '?'})")
