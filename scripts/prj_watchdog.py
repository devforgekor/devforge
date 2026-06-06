#!/usr/bin/env python3
# Status: production
# Path: manual — nohup
"""
P-R-J pipeline watchdog — 자동 오류 감지, 수정, 재시도.

실행: nohup python3 prj_watchdog.py &

전략: phase 전환마다 podman stop으로 모든 컨테이너 제거 후 필요한 것만 시작.
이전 phase의 컨테이너가 메모리를 점유하는 문제 해결.
"""

import json, os, subprocess, sys, time, traceback
from datetime import datetime, timezone

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
EXPER_DIR = os.path.join(SCRIPTS_DIR, "..", "data", "experiment")
os.makedirs(EXPER_DIR, exist_ok=True)

MODE_FILE_B = "/opt/ai_data/scripts/current-mode-pod-b.env"
MODE_FILE_A = "/opt/ai_data/scripts/current-mode-pod-a.env"
LOG = os.path.join(EXPER_DIR, f"watchdog_{datetime.now().strftime('%Y%m%d_%H%M')}.log")


def wlog(msg):
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, "", "TIMEOUT"
    except Exception as e:
        return -1, "", str(e)


def kill_all_recover():
    """모든 podman 컨테이너 제거. 실패 무시."""
    run("podman stop -t 5 devforge-swap", timeout=15)
    run("podman stop -t 5 devforge-pod-a", timeout=15)
    time.sleep(3)


def restore_day():
    """Pod B day(:8080) + Pod A day(:8082) 복원."""
    wlog("  Restoring day mode...")
    run(f"printf '%s' 'MODE=day' > '{MODE_FILE_B}'")
    run(f"printf '%s' 'MODE=day' > '{MODE_FILE_A}'")
    kill_all_recover()
    run("systemctl --user start container-devforge-pod-a", timeout=60)
    run("systemctl --user start container-devforge-swap", timeout=60)
    time.sleep(30)


def fix_and_retry(phase, error_text):
    """Known error patterns -> fix -> return True to retry."""
    wlog(f"  Error [{phase}]: {error_text[:200]}")

    # OOM or memory
    if any(kw in error_text.lower() for kw in ["oom", "memory", "cannot allocate", "no space"]):
        wlog("  OOM. kill_all + wait 10s...")
        kill_all_recover()
        time.sleep(10)
        return True

    # Connection refused/reset (stale container)
    if any(kw in error_text.lower() for kw in ["refused", "reset", "econnrefused", "closed"]):
        wlog("  Connection error. kill_all then retry...")
        kill_all_recover()
        time.sleep(5)
        return True

    # Timeout
    if "timeout" in error_text.lower():
        wlog("  Timeout. Retrying...")
        time.sleep(10)
        return True

    # JSON error (model returned bad output)
    if "json" in error_text.lower() and ("decode" in error_text.lower() or "parse" in error_text.lower()):
        wlog("  JSON error. Retrying LLM call...")
        time.sleep(5)
        return True

    # 503 Service Unavailable (model loading not complete)
    if "503" in error_text or "service unavailable" in error_text.lower():
        wlog("  503 Service Unavailable. kill_all + waiting longer...")
        kill_all_recover()
        time.sleep(30)
        return True

    return False


def run_with_retry(phase, script, max_retries=3, timeout=7200):
    """Run script, auto-fix, retry."""
    for attempt in range(1, max_retries + 1):
        wlog(f"[{phase}] attempt {attempt}/{max_retries}")
        rc, stdout, stderr = run(
            f"cd {SCRIPTS_DIR} && PYTHONPATH={SCRIPTS_DIR} python3 {script}",
            timeout=timeout)
        if rc == 0:
            wlog(f"[{phase}] OK")
            return True
        wlog(f"[{phase}] exit {rc}: {stderr[:300]}")
        if not fix_and_retry(phase, stderr + "\n" + stdout[-500:]):
            wlog(f"[{phase}] Unknown error, retrying anyway...")
    wlog(f"[{phase}] FAILED after {max_retries} attempts")
    return False


def main():
    wlog("=" * 60)
    wlog("P-R-J Watchdog 시작 (kill_all 방식)")
    wlog(f"Time: {datetime.now(timezone.utc).isoformat()}")
    wlog("=" * 60)

    # Step 1: prj_cycle.py (Round 1 no rubric -> Round 2 rubric)
    ok = run_with_retry("prj_cycle", "prj_cycle.py", max_retries=2, timeout=7200)

    # Step 2: 27B optimization test (quick mode)
    ok2 = run_with_retry("27b_test", "test_27b_optimization.py --quick",
                          max_retries=2, timeout=3600)

    # Step 3: restore day mode
    restore_day()

    wlog("\n--- Pipelines done ---")
    wlog(f"prj_cycle: {'OK' if ok else 'FAIL'}")
    wlog(f"27b_test:  {'OK' if ok2 else 'FAIL'}")
    wlog(f"Log: {LOG}")


if __name__ == "__main__":
    main()
