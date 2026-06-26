#!/usr/bin/env python3
# Status: production
"""Container management for DevForge — Pod A and Pod B."""

from __future__ import annotations
import json
import subprocess
import time
import urllib.request
from typing import Optional

from lib.pod_manager.models import MODEL_METADATA, DAY_PHASE_MODELS, NIGHT_MODELS
from lib.pod_manager.container import (
    log, _reclaim_memory, _check_container_health, _check_model_identity,
    _kill_stray_pasta, _write_mode_env,
)

MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
TIMEOUT = 7200


def model_info(key):
    m = MODEL_METADATA.get(key, {})
    return f"{key}({m.get('file','?')} {m.get('size','?')} :{m.get('port','?')})"


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


def kill_all(night=False, dry_run=False):
    if dry_run:
        log("  [DRY] kill_all() skipped")
        return

    _skip_pod_a = False
    try:
        with open(MODE_FILE_A) as f:
            for line in f:
                if line.strip() == "MODE=reranker":
                    _skip_pod_a = True
                    break
    except OSError:
        pass

    if night:
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=10)
        for svc in ("devforge-day-cycle.service",
           "devforge-night-cycle.service", "devforge-night-cycle.timer"):
            subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10)
        _kill_stray_pasta(("8081", "8082", "8083", "8084"))
    else:
        subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=10)
        if not _skip_pod_a:
            subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                           capture_output=True, timeout=30)
            subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                           capture_output=True, timeout=10)
        ports_to_clean = ("8081", "8082", "8083", "8084")
        if not _skip_pod_a:
            ports_to_clean = ("8080",) + ports_to_clean
        _kill_stray_pasta(ports_to_clean)
    _reclaim_memory()


def start_pod_a_only(mode, port, dry_run=False):
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
    log("  Pod A stop (Pod B running)...")
    subprocess.run(["systemctl", "--user", "stop", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    subprocess.run(["systemctl", "--user", "reset-failed", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=30)
    _kill_stray_pasta(("8080",))


def _start_and_wait(port, health_timeout, skip_probe, mode):
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-b.service"],
                   capture_output=True, timeout=60)
    ok = wait_health(port, timeout=health_timeout)
    if not ok:
        log(f"  :{port} health timeout — restarting container (pasta workaround)")
        subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b.service"],
                       capture_output=True, timeout=60)
        ok = wait_health(port, timeout=min(health_timeout, 300))
    if ok and not skip_probe:
        ok = wait_probe(port, mode, timeout=600)
    return ok


def start_pod_b(mode, port, night=False, dry_run=False, skip_probe=False, model_key=None):
    log(f"  POD B -> {mode} (:{port})")
    _write_mode_env(mode, port, model_key=model_key)
    kill_all(night=night, dry_run=dry_run)
    health_timeout = 1200 if night else 600
    _write_mode_env(mode, port, model_key=model_key)
    ok = _start_and_wait(port, health_timeout, skip_probe, mode)
    if ok and model_key:
        if not _check_model_identity(port, model_key):
            log(f"  :{port} wrong model after start — retrying with env re-write")
            _write_mode_env(mode, port, model_key=model_key)
            subprocess.run(["systemctl", "--user", "restart", "container-devforge-pod-b.service"],
                           capture_output=True, timeout=60)
            ok = _start_and_wait(port, min(health_timeout, 300), skip_probe, mode)
            if not ok or not _check_model_identity(port, model_key):
                log(f"  FATAL: :{port} wrong model after retry — continuing anyway")
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
    with open(MODE_FILE_A, "w") as f:
        f.write(f"MODE={mode}")
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
    log("  start_day_both: DEPRECATED — Pod A is reranker-only")
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
                    if _check_model_identity(meta['port'], physical_name):
                        log(f"  :{meta['port']} already healthy and correct model — skip restart")
                        _check_container_health(meta['port'], physical_name)
                        return True
                    else:
                        log(f"  :{meta['port']} healthy but wrong model — restart needed")
        except Exception:
            pass
    if meta["port"] == 8080:
        ok = start_pod_a(meta["mode"], meta["port"], dry_run=dry_run)
    else:
        ok = start_pod_b(meta["mode"], meta["port"], night=night, dry_run=dry_run, model_key=physical_name)
    if ok:
        return True
    log(f"  ensure_model({physical_name}) failed — retrying after GC + 10s")
    _reclaim_memory()
    import gc as _gc
    _gc.collect()
    time.sleep(10)
    if meta["port"] == 8080:
        return start_pod_a(meta["mode"], meta["port"], dry_run=dry_run)
    return start_pod_b(meta["mode"], meta["port"], night=night, dry_run=dry_run, model_key=physical_name)
