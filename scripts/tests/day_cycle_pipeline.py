#!/usr/bin/env python3
# Status: experimental
# Path: none — day cycle pipeline test harness (scan → extract → verify → enrich → embed)
"""Day cycle pipeline test harness.

Usage:
  python3 scripts/pipelines/day_cycle_pipeline.py --test --limit 10 --runs 2
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from lib.infra.preflight import preflight_checks
from lib.test_common import test_complete, test_setup


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def run_pipeline_file(py_file: str, limit: int, extra_args: list = None) -> dict:
    """Run a pipeline .py file as subprocess, return timing + exit info."""
    cmd = [sys.executable, "-u", py_file, "--limit", str(limit)]
    if extra_args:
        cmd.extend(extra_args)
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        elapsed = time.monotonic() - t0
        return {
            "exit": r.returncode,
            "elapsed_s": round(elapsed, 1),
            "stdout_tail": r.stdout.strip().splitlines()[-5:] if r.stdout else [],
            "stderr": r.stderr[:300] if r.stderr else "",
            "ok": r.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - t0
        return {
            "exit": -1,
            "elapsed_s": round(elapsed, 1),
            "stdout_tail": [],
            "stderr": "TIMEOUT",
            "ok": False,
        }
    except Exception as e:
        return {
            "exit": -2,
            "elapsed_s": 0,
            "stdout_tail": [],
            "stderr": str(e),
            "ok": False,
        }


def score_speed(phases: list) -> tuple:
    """Score speed: each phase under expected threshold = full points."""
    thresholds = {"embed": 120, "extract": 300, "enrich": 300, "verify": 600}
    total = 0.0
    max_p = len(phases) * 25
    for p in phases:
        # time ratio: phase_elapsed / threshold, capped at 3x
        name = p["name"]
        elapsed = p.get("elapsed_s", 999)
        th = thresholds.get(name, 300)
        ratio = elapsed / th
        if ratio <= 0.5:
            total += 25
        elif ratio <= 1.0:
            total += 25 * (1 - (ratio - 0.5) / 0.5)
        elif ratio <= 2.0:
            total += 12.5 * (1 - (ratio - 1.0))
        # else 0
    return round(total), f"{total:.0f}/{max_p}"


def score_stability(results: list) -> int:
    """Score stability: if multiple runs, exit code + elapsed consistency."""
    if len(results) <= 1:
        return 50  # single run = partial
    ok_count = sum(1 for r in results if r.get("exit") == 0)
    ok_ratio = ok_count / len(results)
    # Elapsed variance
    elapsed_list = [r.get("elapsed_s", 0) for r in results]
    avg_e = sum(elapsed_list) / len(elapsed_list)
    var = sum((e - avg_e) ** 2 for e in elapsed_list) / len(elapsed_list) if avg_e > 0 else 999
    cv = (var**0.5) / avg_e if avg_e > 0 else 1.0
    stability = ok_ratio * 60 + max(0, 40 - int(cv * 100))
    return min(100, int(stability))


def print_score_table(run_results: list) -> None:
    """Print consolidated score table from all runs."""
    print("=" * 70)
    print("  SCORE TABLE")
    print("=" * 70)

    # Per-run phase scores
    all_phase_names = ["embed", "extract", "enrich", "verify"]
    phase_ok = {n: [] for n in all_phase_names}
    phase_elapsed = {n: [] for n in all_phase_names}

    for ri, rr in enumerate(run_results, 1):
        for ph in rr.get("phases", []):
            pname = ph["name"]
            phase_ok.setdefault(pname, []).append(ph.get("ok", False))
            phase_elapsed.setdefault(pname, []).append(ph.get("elapsed_s", 0))
        exit_code = rr.get("exit", -1)
        exit_emoji = "OK" if exit_code == 0 else f"FAIL({exit_code})"
        total_s = rr.get("elapsed_s", 0)
        phases_str = ", ".join(
            f"{p['name']}={p['elapsed_s']:.0f}s{'✓' if p.get('ok') else '✗'}"
            for p in rr.get("phases", [])
        )
        print(f"  Run {ri}: {exit_emoji} {total_s:.0f}s [{phases_str}]")

    # Speed score (avg across runs)
    speed_scores = []
    for pn in all_phase_names:
        if phase_elapsed.get(pn):
            avg_e = sum(phase_elapsed[pn]) / len(phase_elapsed[pn])
            s, _ = score_speed([{"name": pn, "elapsed_s": avg_e}])
            speed_scores.append(s)
    speed_score = round(sum(speed_scores) / len(speed_scores)) if speed_scores else 0

    # Stability score
    stability = score_stability(run_results)

    # Quality score: verify pass rate
    verify_ok = sum(1 for v in phase_ok.get("verify", []) if v)
    verify_total = len(phase_ok.get("verify", []))
    quality = round(verify_ok / verify_total * 100) if verify_total > 0 else 0

    # Correctness score: all phases across all runs
    total_ok = 0
    total_ph = 0
    for rr in run_results:
        for ph in rr.get("phases", []):
            total_ph += 1
            if ph.get("ok"):
                total_ok += 1
    correctness = round(total_ok / total_ph * 100) if total_ph > 0 else 0

    # Print table
    print(f"\n  {'Metric':<20s} {'Score':>6s}  Bar")
    print(f"  {'-' * 20} {'-' * 6}  {'-' * 20}")
    for label, val in [
        ("Correctness", correctness),
        ("Speed", speed_score),
        ("Stability", stability),
        ("Quality", quality),
    ]:
        bar = "█" * (val // 10) + "░" * (10 - val // 10)
        grade = "A" if val >= 90 else "B" if val >= 70 else "C" if val >= 50 else "D"
        print(f"  {label:<20s} {val:>4d}/100  {bar} {grade}")

    overall = round((correctness + speed_score + stability + quality) / 4)
    bar = "█" * (overall // 10) + "░" * (10 - overall // 10)
    grade = "A" if overall >= 90 else "B" if overall >= 70 else "C" if overall >= 50 else "D"
    print(f"\n  {'OVERALL':<20s} {overall:>4d}/100  {bar} {grade}")

    # Save JSON
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scores": {
            "correctness": correctness,
            "speed": speed_score,
            "stability": stability,
            "quality": quality,
            "overall": overall,
        },
        "runs": [
            {
                "run": ri + 1,
                "exit": rr.get("exit", -1),
                "elapsed_s": rr.get("elapsed_s", 0),
                "phases": [
                    {
                        "name": p["name"],
                        "ok": p.get("ok", False),
                        "elapsed_s": p.get("elapsed_s", 0),
                    }
                    for p in rr.get("phases", [])
                ],
            }
            for ri, rr in enumerate(run_results)
        ],
    }
    report_path = "/opt/ai_data/day_cycle_test_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  Report saved: {report_path}")


def run_test(limit: int, runs: int) -> None:
    """Run day cycle pipeline test with scoring."""
    print("=" * 60)
    print("  DevForge Day Cycle Test Harness")
    print(f"  limit={limit}, runs={runs}")
    print("=" * 60)

    PIPELINE_DIR = os.path.join(SCRIPTS_DIR, "pipelines")
    all_results = []

    for run_i in range(1, runs + 1):
        print(f"\n{'─' * 60}")
        print(f"  Run {run_i}/{runs}")
        print(f"{'─' * 60}")
        run_start = time.monotonic()
        phases = []

        # Phase 1: Extract (handles model startup via ensure_model inside)
        print("\n  == Phase 1/4: Extract (4B Q8 :8082) ==")
        t0 = time.monotonic()
        r1 = run_pipeline_file(os.path.join(PIPELINE_DIR, "extract.py"), limit)
        phases.append({"name": "extract", "ok": r1["ok"], "elapsed_s": r1["elapsed_s"]})

        # Phase 2: Verify — predicate NLI with Veritas-8B (handles model startup via ensure_model)
        print("\n  == Phase 2/4: Verify (Veritas-8B Q4_K_M :8082) ==")
        t0 = time.monotonic()
        r2 = run_pipeline_file(os.path.join(PIPELINE_DIR, "day_verify.py"), limit)
        phases.append({"name": "verify", "ok": r2["ok"], "elapsed_s": r2["elapsed_s"]})

        # Phase 3: Enrich (handles model startup via ensure_sequential_dual)
        print("\n  == Phase 3/4: Enrich (Qwen3-8B Q4_K_M :8082+:8083) ==")
        t0 = time.monotonic()
        r3 = run_pipeline_file(os.path.join(PIPELINE_DIR, "enrich.py"), limit)
        phases.append({"name": "enrich", "ok": r3["ok"], "elapsed_s": r3["elapsed_s"]})

        # Phase 4: Embed (handles model startup via ensure_model inside)
        print("\n  == Phase 4/4: Embed (8B Q8 :8081) ==")
        t0 = time.monotonic()
        r4 = run_pipeline_file(os.path.join(PIPELINE_DIR, "embed_batch.py"), limit)
        phases.append({"name": "embed", "ok": r4["ok"], "elapsed_s": r4["elapsed_s"]})

        total_s = round(time.monotonic() - run_start, 1)
        exit_code = 0 if all(p["ok"] for p in phases) else 1
        run_result = {
            "run": run_i,
            "exit": exit_code,
            "elapsed_s": total_s,
            "phases": phases,
        }
        all_results.append(run_result)
        print(f"\n  Run {run_i} done: {total_s}s, exit={exit_code}")

    # Print score table
    print_score_table(all_results)


def main() -> None:
    TEST = test_setup("day_cycle_pipeline", "Day cycle pipeline test harness")
    parser = argparse.ArgumentParser(description="Day Cycle Pipeline Test")
    parser.add_argument("--test", action="store_true", help="Run in test mode with scoring")
    parser.add_argument("--limit", type=int, default=10, help="Batch limit per phase")
    parser.add_argument("--runs", type=int, default=2, help="Number of test runs")
    parser.add_argument(
        "--dry-run", action="store_true", help="Simulate execution (skip subprocess)"
    )
    args = parser.parse_args()

    if args.dry_run:
        print("Dry run mode — checking resources only")
        preflight_checks("day_cycle_pipeline.py", required_ports={8081, 8082, 8083})
        print("  [ok] All ports available")
        print("  Phases: extract(:8082) → verify(:8082, Veritas-8B) → enrich(:8082) → embed(:8081)")
        test_complete("dry_run")
        return

    if args.test:
        run_test(args.limit, args.runs)
        test_complete("success")
        return

    # Original behavior: extract → MCP enrich
    log("Legacy mode — extract → MCP enrich only")
    preflight_checks("day_cycle_pipeline.py", required_ports={8082})
    common_args = ["--limit", str(args.limit)]
    LOG_DIR = PIPELINE_DIR = os.path.join(SCRIPTS_DIR, "pipelines")

    log("=== Phase 1: Extract ===")
    r = run_pipeline_file(os.path.join(LOG_DIR, "extract.py"), args.limit)
    log(f"  Extract exit={r['exit']}, {r['elapsed_s']}s")
    for line in r["stdout_tail"]:
        log(f"  {line}")

    log("=== Phase 2: MCP Enrich ===")
    r = run_pipeline_file(os.path.join(LOG_DIR, "enrich.py"), args.limit)
    log(f"  MCP Enrich exit={r['exit']}, {r['elapsed_s']}s")
    for line in r["stdout_tail"]:
        log(f"  {line}")

    test_complete("success")


if __name__ == "__main__":
    main()
