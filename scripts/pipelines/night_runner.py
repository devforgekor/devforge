#!/usr/bin/env python3
# Status: experimental
# Path: called by — exp_runner.py (future), manual CLI
"""Night Runner: Pod A stop → Pod B swap (30B→14B→14B→27B) → night_cycle subprocess → Pod B day 복원.

Usage:
  python3 night_runner.py --run-id <run_id> [--tag r1]
"""

import os, subprocess, sys, time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.container_manager import (
    report_memory, stop_all, wait_health,
)
from lib.pod_manager import (
    kill_all, start_pod_a, start_pod_b, stop_pod_a,
    MODE_FILE_B, MODEL_METADATA,
    ensure_model, NIGHT_MODELS, _write_mode_env,
)


def log(msg):
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


def _stop_pod_a_for_ram():
    """Stop Pod A (7B reviewer) to free ~7.6GB RAM for night models."""
    log("Stopping Pod A (7B:8082) to free RAM for night models...")
    stop_pod_a()


def _swap_pod_b(mode, port, timeout=300):
    """Swap Pod B to a specific mode."""
    log(f"  Swapping Pod B to {mode}:{port}...")
    stop_all()
    _write_mode_env(mode, port)
    subprocess.run(
        ["systemctl", "--user", "start", "container-devforge-pod-b.service"],
        capture_output=True, timeout=60,
    )
    ok = wait_health(port, timeout)
    log(f"  Pod B {mode}:{port} = {'OK' if ok else 'TIMEOUT'}")
    return ok


def main():
    run_id = None
    tag = "r1"
    for i, a in enumerate(sys.argv):
        if a == "--run-id" and i + 1 < len(sys.argv):
            run_id = sys.argv[i + 1]
        if a == "--tag" and i + 1 < len(sys.argv):
            tag = sys.argv[i + 1]

    if not run_id:
        log("ERROR: --run-id required")
        sys.exit(1)

    log("=" * 60)
    log("NIGHT RUNNER")
    log(f"Run ID: {run_id}, Tag: {tag}")
    log("=" * 60)

    # 1. Memory report before starting
    report_memory("before")

    # 2. Stop Pod A to free RAM for night models
    _stop_pod_a_for_ram()

    # 3. Pod B swap sequence: 30B(proposer) → 14B(reflector) → 14B(judge) → 27B(verifier)
    log_phase("Pod B swap: proposer (30B:8081)")
    if not _swap_pod_b("review-p", 8081):
        log("FATAL: proposer model swap failed")
        _restore_day_mode()
        sys.exit(1)

    # 4. Run night_cycle.py
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, "pipelines", "night_cycle.py"),
        "--run-id", run_id,
        "--tag", tag,
    ]
    log(f"Running: {' '.join(cmd)}")
    t0 = time.monotonic()

    # Pipeline manages its own Pod B swaps internally (R=reflector, J=judge, V=verifier)
    # via call_one() -> ensure_model() which triggers mode swap
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=28800)
    elapsed = time.monotonic() - t0

    for line in result.stdout.split("\n"):
        print(line)
    if result.stderr:
        for line in result.stderr.split("\n"):
            print(f"  [ERR] {line}")

    log(f"night_cycle {'OK' if result.returncode == 0 else 'FAILED'} ({elapsed/60:.1f} min)")

    # 5. Restore Pod B to day mode
    _restore_day_mode()

    sys.exit(result.returncode)


def _restore_day_mode():
    """Restore Pod B to day mode (7B:8082)."""
    log("Restoring Pod B to day mode (7B:8082)...")
    stop_all()
    import time as _time
    _time.sleep(2)
    _write_mode_env("day", 8082)
    subprocess.run(
        ["systemctl", "--user", "start", "container-devforge-pod-b.service"],
        capture_output=True, timeout=60,
    )
    ok = wait_health(8080, 180)
    log(f"  Pod B day mode restore = {'OK' if ok else 'TIMEOUT'}")
    return ok


def log_phase(title):
    log(f"\n--- {title} ---")


if __name__ == "__main__":
    main()
