#!/usr/bin/env python3
# Status: experimental
# Path: called by — exp_runner.py (future), manual CLI
"""Day Runner: 컨테이너 확인 → day_pipeline.py subprocess → 종료.

Usage:
  python3 day_runner.py [--tag r1] [--skip-extract]
"""

import os, subprocess, sys, time

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.container_manager import (
    start_pod_a, start_pod_b, report_memory, wait_health,
)


def log(msg):
    t = time.strftime("%H:%M:%S", time.gmtime())
    print(f"[{t}] {msg}", flush=True)


def main():
    tag = "r1"
    skip_extract = "--skip-extract" in sys.argv
    for i, a in enumerate(sys.argv):
        if a == "--tag" and i + 1 < len(sys.argv):
            tag = sys.argv[i + 1]

    log("=" * 60)
    log("DAY RUNNER")
    log(f"Tag: {tag}")
    log("=" * 60)

    # 1. Memory report before starting
    report_memory("before")

    # 2. Ensure Pod A (reserved:8080) + Pod B (extract model:8082) running
    log("Starting Pod A (reserved:8080)...")
    pod_a_ok = start_pod_a(120)
    log(f"  Pod A = {'OK' if pod_a_ok else 'TIMEOUT'}")

    log("Starting Pod B (extract model:8082)...")
    pod_b_ok = start_pod_b(120)
    log(f"  Pod B = {'OK' if pod_b_ok else 'TIMEOUT'}")

    if not pod_b_ok:
        log("FATAL: Pod B not available")
        sys.exit(1)

    # 3. Run day_pipeline.py
    cmd = [
        sys.executable,
        os.path.join(SCRIPTS_DIR, "pipelines", "day_pipeline.py"),
        "--tag", tag,
    ]
    if skip_extract:
        cmd.append("--skip-extract")

    log(f"Running: {' '.join(cmd)}")
    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=28800)
    elapsed = time.monotonic() - t0

    # 4. Print pipeline output
    for line in result.stdout.split("\n"):
        print(line)
    if result.stderr:
        for line in result.stderr.split("\n"):
            print(f"  [ERR] {line}")

    log(f"day_pipeline {'OK' if result.returncode == 0 else 'FAILED'} ({elapsed/60:.1f} min)")
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
