#!/usr/bin/env python3
# Status: production
"""Container management for DevForge — all models run in inference container."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from typing import Optional

from lib.model_registry import DAY_PHASE_MODELS, MODEL_METADATA
from lib.pod_manager.container import (
    INFERENCE_CONTAINER,
    MODE_FILE,
    _check_container_health,
    _check_model_identity,
    _podman_start_inference,
    _podman_stop_inference,
    _reclaim_memory,
    _write_dual_env,
    _write_mode_env,
    log,
)

TIMEOUT = 7200


def model_info(key):
    m = MODEL_METADATA.get(key, {})
    return f"{key}({m.get('file', '?')} {m.get('size', '?')} :{m.get('port', '?')})"


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
    body = json.dumps(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5,
            "temperature": 0.1,
            "stream": False,
        }
    ).encode()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions",
                data=body,
                headers={"Content-Type": "application/json"},
            )
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


def kill_all(dry_run=False):
    """Stop inference container."""
    if dry_run:
        log("  [DRY] kill_all() skipped")
        return

    _podman_stop_inference()
    _reclaim_memory()


def _start_and_wait(port, health_timeout, skip_probe, mode):
    """Start inference container and wait for health."""
    if not _podman_start_inference():
        log(f"  podman start FAILED — :{port} will not be available")
        return False
    ok = wait_health(port, timeout=health_timeout)
    if not ok:
        log(f"  :{port} health timeout — restarting container")
        _podman_stop_inference()
        if not _podman_start_inference():
            return False
        ok = wait_health(port, timeout=min(health_timeout, 300))
    if ok and not skip_probe:
        ok = wait_probe(port, mode, timeout=600)
    return ok


def start_inference(mode, port, dry_run=False, skip_probe=False, model_key=None):
    """Start inference container with the given model mode."""
    log(f"  INFERENCE -> {mode} (:{port})")
    _write_mode_env(mode, port, model_key=model_key)
    if not dry_run:
        kill_all()
    health_timeout = 600
    _write_mode_env(mode, port, model_key=model_key)
    ok = _start_and_wait(port, health_timeout, skip_probe, mode)
    if ok and model_key:
        if not _check_model_identity(port, model_key):
            log(f"  :{port} wrong model after start — retrying with env re-write")
            _write_mode_env(mode, port, model_key=model_key)
            _podman_stop_inference()
            _podman_start_inference()
            ok = _start_and_wait(port, min(health_timeout, 300), skip_probe, mode)
            if ok:
                # Retry identity check with backoff — ARM loads ~70s, may not
                # be ready immediately even after probe passes
                for attempt in range(5):
                    if _check_model_identity(port, model_key):
                        break
                    log(
                        f"  :{port} model identity check #{attempt + 1} failed — waiting {10 * (attempt + 1)}s"
                    )
                    time.sleep(10 * (attempt + 1))
                    _write_mode_env(mode, port, model_key=model_key)
                else:
                    log(f"  FATAL: :{port} wrong model after 5 retries — continuing anyway")
    if ok:
        log(f"  :{port} ready")
        _check_container_health()
        time.sleep(5)
    return ok


def ensure_model(physical_name, skip_if_healthy=False, dry_run=False):
    """Start inference container with the requested model."""
    if dry_run:
        log(f"  [DRY] ensure_model({physical_name}) -> OK (mock)")
        return True
    meta = MODEL_METADATA.get(physical_name)
    if not meta:
        log(f"  Unknown model: {physical_name}")
        return False
    if skip_if_healthy:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{meta['port']}/health")
            with urllib.request.urlopen(req, timeout=3) as r:
                if r.status == 200:
                    if _check_model_identity(meta["port"], physical_name):
                        log(f"  :{meta['port']} already healthy and correct model — skip restart")
                        _check_container_health()
                        return True
                    else:
                        log(f"  :{meta['port']} healthy but wrong model — restart needed")
        except Exception:
            pass
    ok = start_inference(meta["mode"], meta["port"], dry_run=dry_run, model_key=physical_name)
    if ok:
        return True
    log(f"  ensure_model({physical_name}) failed — retrying after GC + 10s")
    _reclaim_memory()
    import gc as _gc

    _gc.collect()
    time.sleep(10)
    return start_inference(meta["mode"], meta["port"], dry_run=dry_run, model_key=physical_name)
