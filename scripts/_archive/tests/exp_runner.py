#!/usr/bin/env python3
# Status: experimental
# Path: none — manual / cron experiment orchestrator
"""5-Phase (2x2+baseline) Experiment Runner — 백그라운드 자동 실행.

Usage:
  python3 scripts/pipelines/exp_runner.py [--phase 0] [--dry-run]

Design (2x2 factorial + baseline):

  Phase 0: 기준선          (S=✗, R=OFF, F=OFF)
  Phase 1: 구조개선         (S=✓, R=OFF, F=OFF)
  Phase 2: 구조+루브릭      (S=✓, R=ON,  F=OFF)
  Phase 3: 구조+피드백      (S=✓, R=OFF, F=ON)
  Phase 4: 풀스택           (S=✓, R=ON,  F=ON)

  S=structural (0-5 scale, evidence, catfish, J_rubric)
  R=rubric, F=feedback
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from lib.test_common import log, test_complete, test_setup

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.experiment_state import ExperimentState, update_state
from lib.infra.container_manager import (
    recover_and_restart,
    report_memory,
)
from lib.runner.metrics import (
    extract_metrics,
    generate_comparison_report,
    send_phase_report,
    slack_send,
    utc_timestamp,
)
from lib.runner.snapshot import apply_transform, restore_snapshot, save_snapshot

EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
ARCHIVE_DIR = os.path.join(SCRIPTS_DIR, "_archive")
os.makedirs(EXPER_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)


def log(msg):
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


# ── Pipeline execution ──────────────────────────────────────────


def run_pipeline(phase):
    """Run prj_cycle.py with extract included. Returns (success, metrics_path)."""
    metrics_path = os.path.join(EXPER_DIR, f"phase{phase}_metrics.json")

    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "pipelines", "prj_cycle.py")]

    log(f"Running: {' '.join(cmd)}")
    t0 = time.monotonic()

    output_path = os.path.join(EXPER_DIR, f"phase{phase}_output.log")
    with open(output_path, "w") as out_f:
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(cmd, stdout=out_f, stderr=subprocess.STDOUT, text=True, env=env)
        TIMEOUT = 28800
        poll_interval = 60
        killed = False
        for _ in range(TIMEOUT // poll_interval):
            try:
                proc.wait(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    break
                continue
        else:
            log(f"  TIMEOUT ({TIMEOUT}s) — sending SIGKILL to {proc.pid}")
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=10)
            killed = True

    elapsed = time.monotonic() - t0
    with open(output_path) as f:
        stdout = f.read()

    success = proc.returncode == 0
    metrics = extract_metrics(stdout, phase, elapsed)
    metrics["returncode"] = proc.returncode
    metrics["elapsed_seconds"] = round(elapsed, 1)
    metrics["success"] = success
    if killed:
        metrics["killed"] = True

    with open(metrics_path, "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    log(f"Phase {phase} {'OK' if success else 'FAILED'} ({elapsed / 60:.1f} min)")
    return success, metrics_path


# ── Phase preparation ───────────────────────────────────────────


def prepare_all_phases():
    """Build phase 0-4 snapshots from original, each with own flag set."""
    log("Preparing all phase snapshots...")

    save_snapshot("_original")

    for p in range(5):
        restore_snapshot("_original")
        apply_transform(p)
        save_snapshot(p)

    restore_snapshot(0)
    log("All phase snapshots ready")


def _restart_services():
    """Restart watchdog stopped by run_experiment()."""
    known_services = {
        "devforge-watchdog.service",
    }
    for unit in sorted(known_services):
        subprocess.run(["systemctl", "--user", "start", unit], capture_output=True, timeout=30)
    log("  Watchdog restarted")


# ── Experiment orchestrator ─────────────────────────────────────


def run_experiment(phases):
    """Run requested phases sequentially."""
    slack_send(
        f":rocket: *실험 시작* (Phase {phases[0]}→{phases[-1]})\n{utc_timestamp()} UTC\n각 phase마다 cache reset"
    )

    for phase in phases:
        log(f"\n{'=' * 60}")
        log(f"PHASE {phase}")
        log(f"{'=' * 60}")
        update_state(current_phase=phase, step=f"phase_{phase}_start")

        success = False
        for attempt in range(1, 4):
            log(f"Attempt {attempt}/3")

            snap_dir = os.path.join(ARCHIVE_DIR, f"phase{phase}")
            if os.path.exists(snap_dir):
                restore_snapshot(phase)
            else:
                log(f"  WARNING: phase{phase} snapshot not found, using phase0")
                restore_snapshot(0)

            slack_send(f":arrows_counterclockwise: *Phase {phase}* (attempt {attempt}/3)")
            report_memory(f"before attempt {attempt}")
            containers_ok = recover_and_restart(attempt=attempt)
            if not containers_ok:
                slack_send(
                    f":fire: *Phase {phase}* (attempt {attempt}) — container recovery failed"
                )
                log(f"  recover_and_restart attempt {attempt} failed")
                continue

            ok, metrics_path = run_pipeline(phase)

            if ok:
                success = True
                send_phase_report(phase, metrics_path)
                labels = {
                    0: "기준선",
                    1: "구조개선",
                    2: "구조+루브릭",
                    3: "구조+피드백",
                    4: "풀스택",
                }
                slack_send(f":bar_chart: *Phase {phase}* {labels.get(phase, '완료')}")
                break
            else:
                log(f"  Phase {phase} attempt {attempt} FAILED")
                if attempt < 3:
                    slack_send(f":warning: *Phase {phase}* (attempt {attempt}) 실패. 재시도 예정.")
                    time.sleep(30)

        if not success:
            slack_send(f":no_entry: *Phase {phase}* — 3회 모두 실패. 실험 중단.")
            _restart_services()
            return False

    generate_comparison_report()
    _restart_services()
    return True


def main():
    TEST = test_setup("exp_runner", "5-Phase (2x2+baseline) Experiment Runner")
    from lib.infra.preflight import preflight_checks

    preflight_checks("exp_runner.py")
    phases = [0, 1, 2, 3, 4]
    dry_run = "--dry-run" in sys.argv

    for i, a in enumerate(sys.argv):
        if a == "--phase" and i + 1 < len(sys.argv):
            phases = [int(sys.argv[i + 1])]

    log("=" * 60)
    log("EXPERIMENT RUNNER")
    log(f"Phases: {phases}")
    log(f"{'=' * 60}")

    if dry_run:
        log("DRY RUN — building phase snapshots only")
        prepare_all_phases()
        return

    prepare_all_phases()

    input_fp = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
    if os.path.exists(input_fp):
        with open(input_fp) as f:
            findings_count = len(json.load(f).get("findings", []))
        log(f"Consolidated input: {input_fp} ({findings_count} findings)")
    else:
        log("No consolidated input file — extract will read from DB")

    try:
        with ExperimentState(phase=phases[0], phases=phases, step="preparing"):
            success = run_experiment(phases)

        restore_snapshot(0)

        if success:
            slack_send(":tada: *실험 완료!*")
        else:
            slack_send(":x: *실험 실패*")

        for phase in phases:
            log(f"  Phase {phase}: {os.path.join(EXPER_DIR, f'phase{phase}_metrics.json')}")
            log(f"  Log: {os.path.join(EXPER_DIR, f'phase{phase}_output.log')}")
        test_complete("experiment done")
    finally:
        _restart_services()


if __name__ == "__main__":
    main()
