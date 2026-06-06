#!/usr/bin/env python3
# Status: experimental
# Path: none — library
"""5-Phase (2x2+baseline) Experiment Runner — 백그라운드 자동 실행.

Usage:
  python3 experiment_runner.py [--phase 0] [--dry-run]

Design (2x2 factorial + baseline):

  Phase 0: 기준선          (S=✗, R=OFF, F=OFF)
  Phase 1: 구조개선         (S=✓, R=OFF, F=OFF)
  Phase 2: 구조+루브릭      (S=✓, R=ON,  F=OFF)
  Phase 3: 구조+피드백      (S=✓, R=OFF, F=ON)
  Phase 4: 풀스택           (S=✓, R=ON,  F=ON)

  S=structural (0-5 scale, evidence, catfish, J_rubric)
  R=rubric, F=feedback

  Key comparisons:
    P1→P2 = rubric marginal effect (F=OFF)
    P1→P3 = feedback marginal effect (R=OFF)
    P2→P4 = feedback effect with rubric
    P3→P4 = rubric effect with feedback
    P0→P4 = total improvement

Flag mapping:
  Phase 0: --rubric-off --feedback-off
  Phase 1: --structural --rubric-off --feedback-off
  Phase 2: --structural --feedback-off          (rubric defaults ON)
  Phase 3: --structural --rubric-off            (feedback defaults ON)
  Phase 4: --structural                         (rubric + feedback default ON)
"""

import json, os, re, shutil, subprocess, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
ARCHIVE_DIR = os.path.join(SCRIPTS_DIR, "_archive")
os.makedirs(EXPER_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)

SLACK_TOKEN = ""
SLACK_CHANNEL = "U0APJGD8CBW"
_sf = Path.home() / ".config/devforge/secrets.env"
if _sf.exists():
    for _line in _sf.read_text().split("\n"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "SLACK_BOT_TOKEN":
                SLACK_TOKEN = _v.strip().strip('"').strip("'")
            elif _k.strip() == "SLACK_CHANNEL":
                SLACK_CHANNEL = _v.strip().strip('"').strip("'")


def log(msg):
    t = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{t}] {msg}", flush=True)


def slack_send(text):
    if not SLACK_TOKEN:
        return
    payload = json.dumps({"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=payload,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            r = json.loads(resp.read())
            if not r.get("ok"):
                log(f"Slack API error: {r.get('error','?')}")
    except Exception as e:
        log(f"Slack send failed: {e}")


def ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Source file management ──────────────────────────────────────

KEY_FILES = ["prj_cycle.py", "extract_pipeline.py", "classify_pipeline.py",
             "nightly_batch.sh", "15m_cycle.sh"]


def save_snapshot(phase):
    d = os.path.join(ARCHIVE_DIR, f"phase{phase}")
    os.makedirs(d, exist_ok=True)
    for fname in KEY_FILES:
        src = os.path.join(SCRIPTS_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(d, fname))
    log(f"Snapshot saved: _archive/phase{phase}/")


def restore_snapshot(phase):
    d = os.path.join(ARCHIVE_DIR, f"phase{phase}")
    for fname in KEY_FILES:
        src = os.path.join(d, fname)
        dst = os.path.join(SCRIPTS_DIR, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)
    log(f"Restored from _archive/phase{phase}/")


# ── Code transformation ─────────────────────────────────────────

PHASE_FLAGS = {
    0: ["--rubric-off", "--feedback-off"],
    1: ["--structural", "--rubric-off", "--feedback-off"],
    2: ["--structural", "--feedback-off"],
    3: ["--structural", "--rubric-off"],
    4: ["--structural"],
}

def apply_transform(phase):
    """Apply phase transformation via external transform_prj.py script."""
    fp = os.path.join(SCRIPTS_DIR, "prj_cycle.py")
    tscript = os.path.join(SCRIPTS_DIR, "transform_prj.py")
    flags = PHASE_FLAGS.get(phase, [])

    if not os.path.exists(tscript):
        log("ERROR: transform_prj.py not found")
        return False

    with open(fp) as f:
        original = f.read()

    cmd = [sys.executable, tscript] + flags
    result = subprocess.run(
        cmd, input=original, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        log(f"transform_prj {' '.join(flags)} failed: {result.stderr}")
        return False

    with open(fp, "w") as f:
        f.write(result.stdout)
    log(f"Phase {phase} transformation applied ({' '.join(flags)})")
    return True


# ── Container management ────────────────────────────────────────

def _stop_all():
    """Stop both LLM containers."""
    for svc in ["container-devforge-swap.service", "container-devforge-pod-a.service"]:
        subprocess.run(["systemctl", "--user", "stop", svc], capture_output=True, timeout=30)
        subprocess.run(["systemctl", "--user", "reset-failed", svc], capture_output=True, timeout=10)


def _free_memory(level=1):
    """공격적 메모리 회수. level:
    1 = sync + 15s 대기 (기본)
    2 = level1 + drop_caches + swap 재활성화
    3 = level2 + 30s 대기 + oom_score_adj 전파
    """
    os.sync()
    log(f"  Memory reclaim level {level}: synced, waiting...")

    if level >= 2:
        # page cache, dentries, inodes 강제 회수 (커널 3.0+)
        try:
            with open("/proc/sys/vm/drop_caches", "w") as f:
                f.write("3\n")
            log("  drop_caches=3 OK")
        except Exception as e:
            log(f"  drop_caches failed (non-fatal): {e}")

        # swap 재활성화: OOM으로 밀려난 페이지를 정리
        try:
            subprocess.run(["swapoff", "-a"], capture_output=True, timeout=30)
            subprocess.run(["swapon", "-a"], capture_output=True, timeout=30)
            log("  swap re-activated OK")
        except Exception as e:
            log(f"  swap reactivate failed (non-fatal): {e}")

    wait_time = 30 if level >= 3 else 15
    for i in range(wait_time):
        if i % 5 == 0:
            _report_mem(f"reclaim ({i}s)")
        time.sleep(1)

    _report_mem("after reclaim")


def _report_mem(label=""):
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


def _get_available_mb():
    """Return MemAvailable in MB, or 0 on error."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        return 0


def _write_mode(pod, mode):
    """Write mode file atomically."""
    path = f"/opt/ai_data/scripts/current-mode-{pod}.env"
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        f.write(f"MODE={mode}")
    os.rename(tmp, path)


def _start_pod_a(timeout=120):
    """Start Pod A (day_r:8082)."""
    log("  Starting Pod A (day_r:8082)...")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-pod-a.service"],
                   capture_output=True, timeout=60)
    return wait_health(8082, timeout)


def _start_pod_b(timeout=120):
    """Start Pod B (day:8080)."""
    log("  Starting Pod B (day:8080)...")
    subprocess.run(["systemctl", "--user", "start", "container-devforge-swap.service"],
                   capture_output=True, timeout=60)
    return wait_health(8080, timeout)


def wait_health(port, timeout=120):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req, timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def recover_and_restart(attempt=1):
    """OOM/실패 후 자율 회복 + day mode 재시작.

    attempt=1: 기본 — stop + sync + 15s + 3B+7B
    attempt=2: 공격 — drop_caches + swap off/on + 3B+7B
    attempt=3: 최후 — minimal mode (day model only, Pod A 없이)
    """
    _stop_all()
    _free_memory(level=min(attempt, 3))
    _write_mode("pod-b", "day")
    _write_mode("pod-a", "day")

    if attempt <= 2:
        log("  Attempt: day_r + day_p/day_j (standard day mode)")
        ok_a = _start_pod_a(120)
        log(f"  Pod A (day_r:8082) = {'OK' if ok_a else 'TIMEOUT'}")

        if not ok_a and attempt == 2:
            _report_mem("after Pod A failure")
            # day_r 실패 → 더 강력한 회수 후 재시도
            _stop_all()
            _free_memory(level=2)
            ok_a = _start_pod_a(120)
            log(f"  Pod A retry (day_r:8082) = {'OK' if ok_a else 'TIMEOUT'}")

        if ok_a:
            ok_b = _start_pod_b(180)
            log(f"  Pod B (day:8080) = {'OK' if ok_b else 'TIMEOUT'}")
            if ok_b:
                return True

        # 3B+7B 실패 → minimal mode: 7B only
        log("  Escalating to minimal mode (day model only)...")

    # attempt=3 or escalation: minimal mode
    _stop_all()
    _free_memory(level=3)
    _write_mode("pod-b", "day")

    # Pod B만 단독 시작 (day_r 없음 — 메모리 3.1GB 절약)
    log("  Minimal mode: Pod B only (day:8080)")
    ok_b = _start_pod_b(300)  # 더 긴 timeout
    if ok_b:
        log("  Minimal mode OK: day model running alone")
        return True

    log("  FATAL: even minimal mode failed")
    return False


# ── Pipeline execution ──────────────────────────────────────────

def run_pipeline(phase):
    """Run prj_cycle.py with --skip-extract. Returns (success, metrics_path)."""
    metrics_path = os.path.join(EXPER_DIR, f"phase{phase}_metrics.json")

    cmd = [sys.executable, os.path.join(SCRIPTS_DIR, "prj_cycle.py"),
           "--skip-extract"]

    log(f"Running: {' '.join(cmd)}")
    t0 = time.monotonic()

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=28800)
    elapsed = time.monotonic() - t0

    log_path = os.path.join(EXPER_DIR, f"phase{phase}_output.log")
    with open(log_path, "w") as f:
        f.write(proc.stdout or "")
        if proc.stderr:
            f.write("\n\n=== STDERR ===\n")
            f.write(proc.stderr)

    success = proc.returncode == 0
    metrics = extract_metrics(proc.stdout, phase, elapsed)
    metrics["returncode"] = proc.returncode
    metrics["elapsed_seconds"] = round(elapsed, 1)
    metrics["success"] = success

    with open(metrics_path, "w") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)

    log(f"Phase {phase} {'OK' if success else 'FAILED'} ({elapsed/60:.1f} min)")
    return success, metrics_path


def extract_metrics(stdout, phase, elapsed):
    metrics = {
        "phase": phase, "timestamp": ts(),
        "elapsed_seconds": round(elapsed, 1),
    }

    for line in stdout.split("\n"):
        if "Python verify:" in line and "issues" in line:
            m = re.search(r'(\d+)\s+issues.*?(\d+)\s+findings', line)
            if m:
                metrics["python_issues"] = int(m.group(1))
                metrics["python_findings"] = int(m.group(2))

        if "day_verify verdict=" in line:
            m = re.search(r'verdict=(\S+)\s+confidence=(\S+)', line)
            if m:
                metrics["day_verify_verdict"] = m.group(1)
                metrics["day_verify_confidence"] = m.group(2)

        if "avg weighted_score=" in line:
            m = re.search(r'avg weighted_score=([\d.]+)', line)
            if m:
                metrics["rubric_avg_score"] = float(m.group(1))
        if "Evaluated" in line and "findings" in line:
            m = re.search(r'Evaluated (\d+) findings', line)
            if m:
                metrics["rubric_evaluated_count"] = int(m.group(1))

        if "P_score=" in line and "R_score=" in line and "decision=" in line and "J_classify" not in line:
            m = re.search(r'P_score=(\S+)\s+R_score=(\S+).*?decision=(\S+)', line)
            if m:
                metrics["P_score"] = m.group(1)
                metrics["R_score"] = m.group(2)
                metrics["decision"] = m.group(3)
            m2 = re.search(r'consensus=(\S+)?', line)
            if m2 and m2.group(1):
                metrics["consensus"] = m2.group(1)

        if "night_verify verdict=" in line:
            m = re.search(r'verdict=(\S+)\s+confidence=(\S+)', line)
            if m:
                metrics["night_verify_verdict"] = m.group(1)
                metrics["night_verify_confidence"] = m.group(2)
        if "night_verify (feedback)" in line:
            m = re.search(r'verdict=(\S+)\s+confidence=(\S+)', line)
            if m:
                metrics["feedback_nv_verdict"] = m.group(1)
                metrics["feedback_nv_confidence"] = m.group(2)

        if "feedback loop executed" in line.lower():
            metrics["feedback_executed"] = True

        if "handoff preference:" in line:
            m = re.search(r'handoff preference:\s+(\S+)', line)
            if m:
                metrics["handoff_preference"] = m.group(1)

        if "processed," in line and "failed" in line:
            m = re.search(r'(\d+)\s+processed,\s+(\d+)\s+failed', line)
            if m:
                metrics["processed"] = int(m.group(1))
                metrics["failed"] = int(m.group(2))

    return metrics


# ── Slack reporting ─────────────────────────────────────────────

def send_phase_report(phase, metrics_path):
    if not os.path.exists(metrics_path):
        slack_send(f":warning: *Phase {phase}* — metrics file not found")
        return
    with open(metrics_path) as f:
        m = json.load(f)
    elapsed = m.get("elapsed_seconds", 0)
    elapsed_min = round(elapsed / 60, 1)

    lines = [f"*Phase {phase}* ({elapsed_min}분)"]
    if "python_issues" in m:
        lines.append(f"▸ Python verify: {m['python_issues']} issues / {m['python_findings']} findings")
    if "day_verify_verdict" in m:
        lines.append(f"▸ day_verify: *{m['day_verify_verdict']}* (conf={m['day_verify_confidence']})")
    if "rubric_evaluated_count" in m:
        lines.append(f"▸ Rubric: {m['rubric_evaluated_count']} findings evaluated, avg={m.get('rubric_avg_score','?')}")
    if "P_score" in m:
        lines.append(f"▸ P-R-J: P={m['P_score']} R={m['R_score']} → *{m['decision']}*")
    if "night_verify_verdict" in m:
        lines.append(f"▸ night_verify: *{m['night_verify_verdict']}* (conf={m['night_verify_confidence']})")
    if m.get("feedback_executed"):
        fb_v = m.get("feedback_nv_verdict", "?")
        fb_c = m.get("feedback_nv_confidence", "?")
        lines.append(f"▸ Feedback loop: night_verify re-verify *{fb_v}* (conf={fb_c})")
    if m.get("success"):
        lines.append(f":white_check_mark: Phase {phase} 성공")
    else:
        lines.append(f":x: Phase {phase} 실패 (exit={m.get('returncode','?')})")
    slack_send("\n".join(lines))


# ── Phase preparation ───────────────────────────────────────────

def prepare_all_phases():
    """Build phase 0-4 snapshots from original, each with own flag set."""
    log("Preparing all phase snapshots...")

    # Save original as base for building phases
    save_snapshot("_original")

    for p in range(5):
        restore_snapshot("_original")
        apply_transform(p)
        save_snapshot(p)

    # Restore Phase 0 snapshot for execution start
    restore_snapshot(0)
    log("All phase snapshots ready")


# ── Experiment orchestrator ─────────────────────────────────────

def run_experiment(phases):
    """Run requested phases sequentially."""
    slack_send(f":rocket: *실험 시작* (Phase {phases[0]}→{phases[-1]})\n{ts()} UTC\n각 phase마다 cache reset")

    for phase in phases:
        log(f"\n{'='*60}")
        log(f"PHASE {phase}")
        log(f"{'='*60}")

        success = False
        for attempt in range(1, 4):
            log(f"Attempt {attempt}/3")

            snap_dir = os.path.join(ARCHIVE_DIR, f"phase{phase}")
            if os.path.exists(snap_dir):
                restore_snapshot(phase)
            else:
                log(f"  WARNING: phase{phase} snapshot not found, using phase0")
                restore_snapshot(0)

            # 2. Memory recovery with escalation (1=mild, 2=aggressive, 3=minimal)
            slack_send(f":arrows_counterclockwise: *Phase {phase}* (attempt {attempt}/3)")
            _report_mem(f"before attempt {attempt}")
            containers_ok = recover_and_restart(attempt=attempt)
            if not containers_ok:
                slack_send(f":fire: *Phase {phase}* (attempt {attempt}) — container recovery failed")
                log(f"  recover_and_restart attempt {attempt} failed")
                continue

            ok, metrics_path = run_pipeline(phase)

            if ok:
                success = True
                send_phase_report(phase, metrics_path)
                labels = {0: "기준선", 1: "구조개선", 2: "구조+루브릭", 3: "구조+피드백", 4: "풀스택"}
                slack_send(f":bar_chart: *Phase {phase}* {labels.get(phase, '완료')}")
                break
            else:
                log(f"  Phase {phase} attempt {attempt} FAILED")
                if attempt < 3:
                    slack_send(f":warning: *Phase {phase}* (attempt {attempt}) 실패. 재시도 예정.")
                    time.sleep(30)

        if not success:
            slack_send(f":no_entry: *Phase {phase}* — 3회 모두 실패. 실험 중단.")
            return False

    generate_comparison_report()
    return True


def generate_comparison_report():
    """Build and send 2x2 factorial comparison across 5 phases."""
    report = {"timestamp": ts(), "phases": {}}
    for phase in range(5):
        mp = os.path.join(EXPER_DIR, f"phase{phase}_metrics.json")
        if os.path.exists(mp):
            with open(mp) as f:
                report["phases"][f"phase{phase}"] = json.load(f)

    cp = os.path.join(EXPER_DIR, "experiment_comparison.json")
    with open(cp, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Slack table — 5 columns
    labels_display = ["기준선", "구조개선", "+루브릭", "+피드백", "풀스택"]
    header = "메트릭 | " + " | ".join(labels_display)
    separator = "---|" + "|".join("---" for _ in range(5))

    lines = [":chart_with_upwards_trend: *2x2 실험 비교*"]
    lines.append(header)
    lines.append(separator)

    for label, key in [
        ("소요시간(min)", None), ("P score", "P_score"), ("R score", "R_score"),
        ("결정", "decision"), ("day_verify conf", "day_verify_confidence"),
        ("night_verify verdict", "night_verify_verdict"), ("night_verify conf", "night_verify_confidence"),
        ("루브릭 평가", "rubric_evaluated_count"), ("피드백 실행", "feedback_executed"),
    ]:
        vals = []
        for p in range(5):
            pd = report["phases"].get(f"phase{p}", {})
            if key is None:
                v = f"{pd.get('elapsed_seconds',0)/60:.0f}m" if pd.get('elapsed_seconds') else "-"
            else:
                v = pd.get(key, "-")
                if isinstance(v, float):
                    v = f"{v:.1f}"
            vals.append(str(v))
        lines.append(f"{label} | {' | '.join(vals)}")

    # 2x2 marginal effects (computed from elapsed time as proxy)
    lines.append("")
    lines.append("*2x2 효과 분석 (소요시간 기준)*")
    elapsed = {}
    for p in range(5):
        pd = report["phases"].get(f"phase{p}", {})
        elapsed[p] = pd.get("elapsed_seconds", 0)

    p0, p1 = elapsed.get(0, 0), elapsed.get(1, 0)
    p2, p3, p4 = elapsed.get(2, 0), elapsed.get(3, 0), elapsed.get(4, 0)

    e_structural = (p1 - p0) / 60 if p0 else 0
    e_rubric_off = (p2 - p1) / 60 if p1 else 0  # rubric effect when F=OFF
    e_feedback_off = (p3 - p1) / 60 if p1 else 0  # feedback effect when R=OFF
    e_feedback_on = (p4 - p2) / 60 if p2 else 0   # feedback effect when R=ON
    e_rubric_on = (p4 - p3) / 60 if p3 else 0     # rubric effect when F=ON

    effects = [
        ("구조개선 (P1-P0)", f"{e_structural:+.1f}분"),
        ("Rubric (P2-P1, F=OFF)", f"{e_rubric_off:+.1f}분"),
        ("Feedback (P3-P1, R=OFF)", f"{e_feedback_off:+.1f}분"),
        ("Feedback (P4-P2, R=ON)", f"{e_feedback_on:+.1f}분"),
        ("Rubric (P4-P3, F=ON)", f"{e_rubric_on:+.1f}분"),
    ]
    for label, val in effects:
        lines.append(f"▸ {label}: {val}")

    slack_send("\n".join(lines))
    log(f"Comparison: {cp}")
    return cp


def main():
    phases = [0, 1, 2, 3, 4]
    dry_run = "--dry-run" in sys.argv

    for i, a in enumerate(sys.argv):
        if a == "--phase" and i + 1 < len(sys.argv):
            phases = [int(sys.argv[i + 1])]

    log("=" * 60)
    log("EXPERIMENT RUNNER")
    log(f"Phases: {phases}")
    log(f"{'='*60}")

    if dry_run:
        log("DRY RUN — building phase snapshots only")
        prepare_all_phases()
        return

    # Step 1: build all snapshots
    prepare_all_phases()

    # Step 2: verify input
    input_fp = os.path.join(SCRIPTS_DIR, "..", "pipeline_input", "consolidated_input_compact.json")
    if not os.path.exists(input_fp):
        slack_send(":no_entry: input file not found")
        sys.exit(1)
    with open(input_fp) as f:
        findings_count = len(json.load(f).get("findings", []))
    log(f"Input: {input_fp} ({findings_count} findings)")

    # Step 3: run
    success = run_experiment(phases)

    # Step 4: restore original
    restore_snapshot(0)

    if success:
        slack_send(":tada: *실험 완료!*")
    else:
        slack_send(":x: *실험 실패*")

    for phase in phases:
        log(f"  Phase {phase}: {os.path.join(EXPER_DIR, f'phase{phase}_metrics.json')}")
        log(f"  Log: {os.path.join(EXPER_DIR, f'phase{phase}_output.log')}")


if __name__ == "__main__":
    main()
