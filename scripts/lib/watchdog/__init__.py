# Status: production
# Path: imported by — watchdog.py (entry point only)
"""DevForge Watchdog — 통합 서버 모니터링/자동복구 데몬.

  - T1+T2 LLM probe (:8080 reserved, :8082 7B Q8, :8083 14B Q6_K)
  - day_cycle.sh 파이프라인 감시 (system sync → embed → extract → verify)
  - night_cycle 타이머 감시 (day 중 kick 생략)
  - 시스템 리소스 (swap, memory, disk)
  - Day pipeline 실패 → :8082 fix loop

MODE=night (능동형, 60s 주기):
  - T1+T2 LLM probe (night 전용 :8084 27B + 공통)
  - Night Debate 진도 감시 (night_cycle.py)
  - Night Verify 진도 감시 (review_consumer.py)
  - Proxy Audit 진도 감시 (proxy_reviewer.py)
  - 각 phase 실패 → watchdog fix loop
  - 임시 podman 검증 모드 관리

Slack heartbeat: 30분 (Block Kit in-place)
"""

import argparse
import importlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from .checker import *
from .config import CHECK_INTERVAL, HEARTBEAT_INTERVAL, ALERT_ONLY_TARGETS, TIMER_TARGETS
from .notifier import heartbeat, send_alert, send_recovery
from .recovery import (
    graduated_recover, recover_service, kill_stale_process,
)
from .state import WatchdogState
from .messenger import log_message, get_undelivered
from lib.experiment_state import (
    cleanup_stale, is_experiment_active, is_experiment_stale,
    read_state as read_experiment_state,
    update_state as update_exp_state,
)


# ── Globals ─────────────────────────────────────────────────────────

_state = WatchdogState()
_running = True
_start_time = time.monotonic()


def log(msg: str) -> None:
    utc_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{utc_timestamp}] {msg}", flush=True)


def sigterm_handler(signum, frame):
    global _running
    log("SIGTERM received, shutting down...")
    _running = False


def sighup_handler(signum, frame):
    log("SIGHUP received, reloading config...")
    importlib.reload(sys.modules.get("lib.watchdog.config"))
    from .checker import read_mode
    _state.set_mode(read_mode())
    log(f"config reloaded, mode={_state.mode}")


def sigusr1_handler(signum, frame):
    """SIGUSR1 — dump current state to journal."""
    log(f"=== Watchdog status dump ===")
    log(f"  mode={_state.mode}, running={_running}, uptime={int(time.monotonic() - _start_time)}s")
    for s in _state.all_summaries():
        log(f"  {s['name']:25s} state={s['state']:10s} fails={s['fail_count']} consecutive={s['consecutive_fail']} circuit={s['circuit_open']}")
    log(f"  events_in_buffer={len(_state._events)}")
    log(f"==============================")


# ── 공통 체크 ──────────────────────────────────────────────────────

def _run_services(results: dict, dry_run: bool):
    """서비스 상태 체크 + 필요시 graduated recovery."""
    for svc in check_all_services():
        tracker = _state.get(f"svc:{svc['name']}")
        if svc["ok"]:
            tracker.record_success()
        elif not dry_run and not is_experiment_active():
            graduated_recover(
                svc["name"], tracker,
                lambda n=svc["name"]: recover_service(n),
            )
            if tracker.is_degraded() and tracker.can_alert():
                send_alert(f"svc:{svc['name']}", tracker.state.value, svc["detail"])
                _state.add_event(f"svc:{svc['name']}", "down", svc["detail"])
        else:
            if tracker.record_failure() and tracker.can_alert():
                send_alert(f"svc:{svc['name']}", tracker.state.value, svc["detail"])
                _state.add_event(f"svc:{svc['name']}", "down", svc["detail"])
        results["services"].append(svc)


def _run_timers(results: dict, dry_run: bool, mode: str = "day"):
    """타이머 지연 체크 + 지연시 kick.

    Mode-aware: night-only timers are NOT kicked during day mode and vice versa.
    """
    night_timers = {"devforge-night-cycle.timer"}
    day_timers = {"devforge-day-cycle.timer"}

    for timer in check_all_timers():
        tracker = _state.get(f"timer:{timer['name']}")
        if timer["ok"]:
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
                send_alert(f"timer:{timer['name']}", "DELAY", timer["detail"])
                _state.add_event(f"timer:{timer['name']}", "delay", timer["detail"])

            # Timer kick: skip if not relevant to current mode
            if not dry_run and tracker.consecutive_fail >= 2:
                if mode == "day" and timer["name"] in night_timers:
                    pass  # night timer, skip during day
                elif mode == "night" and timer["name"] in day_timers:
                    pass  # day timer, skip during night
                else:
                    svc_name = timer["name"].replace(".timer", ".service")
                    log(f"  kicking {svc_name} (timer delayed {timer['detail']})")
                    subprocess.run(
                        ["systemctl", "--user", "start", svc_name],
                        capture_output=True, timeout=10,
                    )
        results["timers"].append(timer)


def _run_memory_check(results: dict):
    """메모리/swap 체크 — alert only, no recovery."""
    mem_ok, mem_info = check_memory()
    mem_tracker = _state.get("system:memory")
    if mem_ok:
        mem_tracker.record_success()
    else:
        if mem_tracker.record_failure() and mem_tracker.can_alert():
            send_alert("system:memory", mem_tracker.state.value,
                       f"mem={mem_info['pct']}% swap={mem_info['swap_pct']}%")
            _state.add_event("system:memory", "crit", f"{mem_info['pct']}%/{mem_info['swap_pct']}%")
    results["memory"] = mem_info


def _run_alert_only(dry_run: bool, results: dict):
    """Alert-only 서비스 체크 (복구 없음)."""
    for name in ALERT_ONLY_TARGETS:
        ok, detail = check_service(name)
        tracker = _state.get(f"svc:{name}")
        if ok:
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
                send_alert(f"svc:{name}", tracker.state.value, detail)
                _state.add_event(f"svc:{name}", "down", detail)
        results.setdefault("services", []).append({"name": name, "ok": ok, "detail": detail})


def _run_common_checks(results: dict, dry_run: bool, mode: str = "day"):
    """모드 공통 체크 — 서비스, 타이머, 메모리, alert-only."""
    _run_services(results, dry_run)
    _run_timers(results, dry_run, mode)
    _run_memory_check(results)
    _run_alert_only(dry_run, results)


# ── Day checks ──────────────────────────────────────────────────────

def run_day_checks(dry_run: bool = False) -> dict:
    """Day mode checks (관찰형)."""
    results = {"containers": [], "services": [], "timers": [],
               "probes": [], "memory": {}, "pipeline_running": False}

    for probe in check_all_llm():
        name = probe["name"]
        tracker = _state.get(f"llm:{name}")
        ok = probe["t1_ok"] and probe["t2_ok"]

        if ok:
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
                detail = f"T1={probe['t1_detail']} T2={probe['t2_detail']}"
                send_alert(f"llm:{name}", tracker.state.value, detail)
                _state.add_event(f"llm:{name}", "state_change", detail)

        lat_ok, lat_detail = check_probe_latency(probe["port"])
        if not lat_ok and lat_detail != "skip":
            _state.add_event(f"llm:{name}", "latency_warn", lat_detail)
            if tracker.can_alert():
                send_alert(f"llm:{name}", "LATENCY", lat_detail)

        results["probes"].append(probe)

    pipe_name, _ = check_pipeline("day_cycle.py")
    results["pipeline_running"] = pipe_name

    _run_common_checks(results, dry_run, "day")
    return results


def run_night_checks(dry_run: bool = False) -> dict:
    """Night mode checks (능동형)."""
    results = {"containers": [], "services": [], "timers": [],
               "probes": [], "memory": {}, "pipeline_running": False}

    for probe in check_all_llm():
        name = probe["name"]
        tracker = _state.get(f"llm:{name}")
        ok = probe["t1_ok"] and probe["t2_ok"]

        if ok:
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
                send_alert(f"llm:{name}", tracker.state.value, f"T1={probe['t1_detail']} T2={probe['t2_detail']}")
                _state.add_event(f"llm:{name}", "fail", probe["t2_detail"])
        results["probes"].append(probe)

    for phase_name, pattern in [
        ("night_cycle", "night_cycle.py"),
        ("review_consumer", "review_consumer.py"),
        ("proxy_reviewer", "proxy_reviewer.py"),
    ]:
        running, pid = check_pipeline(pattern)
        tracker = _state.get(f"pipeline:{phase_name}")
        if running:
            _state.add_event(f"pipeline:{phase_name}", "running", f"PID {pid}")
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
                send_alert(f"pipeline:{phase_name}", "STOPPED", f"no process found")
                _state.add_event(f"pipeline:{phase_name}", "stopped", "")
        results["pipeline_running"] = results["pipeline_running"] or running

    _run_common_checks(results, dry_run, "night")
    return results


# ── Heartbeat ──────────────────────────────────────────────────────

def build_heartbeat_summary(day_results: dict) -> dict:
    """Build summary dict for 30min heartbeat."""
    mode = read_mode()
    experiment_active = is_experiment_active()

    containers = []
    for probe in day_results.get("probes", []):
        ok = probe["t1_ok"] and probe["t2_ok"]
        containers.append({
            "name": probe["name"],
            "port": probe["port"],
            "mode": probe.get("name", "?"),
            "ok": ok,
            "uptime": probe.get("t2_detail", ""),
        })

    services = []
    for svc in day_results.get("services", []):
        services.append({
            "name": svc["name"],
            "detail": "OK" if svc["ok"] else "DOWN",
        })

    timers = day_results.get("timers", [])
    mem = day_results.get("memory", {})
    if mem:
        mem["swap_used_gb"] = round(mem.get("swap_used_mb", 0) / 1024, 1)
        mem["swap_total_gb"] = round(mem.get("swap_total_mb", 0) / 1024, 1)

    # Collect LLM metrics and slots from all running ports
    metrics = {}
    slots = {}
    for probe in day_results.get("probes", []):
        port = probe["port"]
        try:
            metrics[str(port)] = check_llm_metrics(port)
            slots[str(port)] = check_llm_slots(port)
        except Exception:
            pass

    return {
        "mode": mode,
        "experiment_active": experiment_active,
        "containers": containers,
        "services": services,
        "timers": timers,
        "memory": mem,
        "probes": day_results.get("probes", []),
        "metrics": metrics,
        "slots": slots,
        "events_30m": _state.events_since(1800),
    }


# ── Fix Loops ──────────────────────────────────────────────────────

def _fix_loop_common(pipe: str, llm_port: int):
    """Run fix loop for a pipeline that failed consecutively."""
    from .fixloop import run_fix_loop

    tracker = _state.get(f"pipeline:{pipe}")
    if tracker.consecutive_fail >= 2 and tracker.can_retry():
        log(f"  {pipe} failed {tracker.consecutive_fail}x, launching fix loop ({llm_port})")
        # Prefer error_log from experiment state file if experiment is active
        exp = read_experiment_state()
        if exp and exp.get("error_log"):
            error_log = exp["error_log"]
        else:
            error_log = f"pipeline:{pipe} failed {tracker.consecutive_fail} consecutive times"
        result = run_fix_loop(error_log, f"pipeline/{pipe}", llm_port=llm_port)
        if result["fixed"]:
            _state.add_event(f"fix:{pipe}", "fixed", result["detail"])
            send_recovery(f"fix:{pipe}", result["detail"])
            # Clear error_log in experiment state after fix
            if exp:
                update_exp_state(error_log=None, fix_attempts=0)
        else:
            _state.add_event(f"fix:{pipe}", "failed", result["detail"])
            # Update experiment state with failed fix attempt
            if exp:
                update_exp_state(fix_attempts=exp.get("fix_attempts", 0) + 1)


def day_fix_loop():
    """Day mode: fix loop for day_cycle.py failures (Pod A :8080)."""
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)


def night_fix_loop():
    """Night mode: fix loop for night pipeline failures (Pod B :8081)."""
    for pipe in ("night_cycle", "review_consumer", "proxy_reviewer"):
        _fix_loop_common(pipe, llm_port=8081)


# ── Main Loop ───────────────────────────────────────────────────────

def main_loop(one_shot: bool = False, dry_run: bool = False):
    global _running

    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)
    signal.signal(signal.SIGHUP, sighup_handler)
    signal.signal(signal.SIGUSR1, sigusr1_handler)

    kill_stale_process("watchdog.py")

    log(f"Watchdog started (interval={CHECK_INTERVAL}s, dry_run={dry_run})")
    log(f"Initial mode: {read_mode()}")

    _state.set_mode(read_mode())

    while _running:
        loop_start = time.monotonic()
        mode = read_mode()
        _state.set_mode(mode)

        # Check if an experiment is running → monitor-only mode (no recovery)
        experiment_active = is_experiment_active()
        if experiment_active:
            log("  Experiment detected — monitor-only mode (no recovery/fix loops)")

        # Stale experiment cleanup — PID died but state file remains
        if is_experiment_stale():
            log("  Stale experiment state detected — cleaning up")
            _state.add_event("experiment", "stale_cleanup", "")
            cleanup_stale()

        results = {}
        if mode == "day":
            msgs = get_undelivered("operator")
            for m in msgs:
                log(f"[TO_OPERATOR] {m['type']}: {m['content']}")
        try:
            if mode == "night":
                results = run_night_checks(dry_run=dry_run)
                log(f"night check done")
                if not dry_run and not experiment_active:
                    night_fix_loop()
            else:
                results = run_day_checks(dry_run=dry_run)
                log(f"day check done")
                if not dry_run and not experiment_active:
                    day_fix_loop()
        except Exception as e:
            log(f"Check cycle error: {e}")
            import traceback
            traceback.print_exc()

        elapsed_since_start = time.monotonic() - _start_time
        if elapsed_since_start > 300 and _state.should_heartbeat(HEARTBEAT_INTERVAL):
            try:
                summary = build_heartbeat_summary(results)
                heartbeat(summary)
                log("heartbeat sent")
            except Exception as e:
                log(f"heartbeat error: {e}")

        if one_shot:
            break

        elapsed = time.monotonic() - loop_start
        sleep_sec = max(1, CHECK_INTERVAL - int(elapsed))
        time.sleep(sleep_sec)

    if not one_shot:
        log("Watchdog stopped")


def main():
    parser = argparse.ArgumentParser(description="DevForge Watchdog")
    parser.add_argument("--one-shot", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Check only, no recovery")
    args = parser.parse_args()
    main_loop(one_shot=args.one_shot, dry_run=args.dry_run)
