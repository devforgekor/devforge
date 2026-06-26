# Status: production
# Path: imported by — watchdog.py (entry point only)
"""DevForge Watchdog — 통합 서버 모니터링/자동복구 데몬.

  - T1+T2 LLM probe (:8080 reranker, :8082, :8083)
  - day_cycle.sh 파이프라인 감시 (system sync → embed → extract → verify)
  - night_cycle 타이머 감시 (day 중 kick 생략)
  - 시스템 리소스 (swap, memory, disk)
  - Day pipeline 실패 → :8082 fix loop

MODE=night (능동형, 60s 주기):
  - T1+T2 LLM probe (night 전용 :8084 verifier + 공통)
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
from lib.db import psql_json
from .notifier import heartbeat, send_alert, send_recovery
from .recovery import (
    graduated_recover, recover_service, kill_stale_process, recover_oom,
    recover_slot_deadlock,
)
from .state import WatchdogState
from .messenger import log_message, get_undelivered, resolve_pulse
from lib.experiment_state import (
    cleanup_stale, is_experiment_active, is_experiment_stale,
    read_state as read_experiment_state,
    update_state as update_exp_state,
)


# ── Globals ─────────────────────────────────────────────────────────

_state = WatchdogState()
_running = True
_start_time = time.monotonic()
_test_active = False  # set per-cycle in main_loop


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
    global _test_active

    for svc in check_all_services():
        tracker = _state.get(f"svc:{svc['name']}")
        if svc["ok"]:
            tracker.record_success()
        elif not dry_run and not is_experiment_active():
            graduated_recover(
                svc["name"], tracker,
                lambda n=svc["name"]: recover_service(n),
            )
            if not _test_active and tracker.is_degraded() and tracker.can_alert():
                send_alert(f"svc:{svc['name']}", tracker.state.value, svc["detail"])
                _state.add_event(f"svc:{svc['name']}", "down", svc["detail"])
        else:
            if tracker.record_failure() and tracker.can_alert():
                if not _test_active:
                    send_alert(f"svc:{svc['name']}", tracker.state.value, svc["detail"])
                    _state.add_event(f"svc:{svc['name']}", "down", svc["detail"])
        results["services"].append(svc)


def _run_timers(results: dict, dry_run: bool, mode: str = "day"):
    """타이머 지연 체크 + 지연시 kick.

    Mode-aware: night-only timers are NOT kicked during day mode and vice versa.
    day-cycle timer is intentionally NOT in TIMER_TARGETS — watchdog triggers
    day_cycle.sh based on DB data (watchdog_pulses), not systemd timer.
    """
    night_timers = {"devforge-night-cycle.timer"}
    day_timers = set()  # Async pipeline — watchdog manages cycle timing directly

    for timer in check_all_timers():
        tracker = _state.get(f"timer:{timer['name']}")
        if timer["ok"]:
            tracker.record_success()
        else:
            # Protection active → test/pipeline deliberately occupying ports → skip alert
            protected = _test_active
            if not protected:
                if tracker.record_failure() and tracker.can_alert():
                    send_alert(f"timer:{timer['name']}", "DELAY", timer["detail"])
                    _state.add_event(f"timer:{timer['name']}", "delay", timer["detail"])

            # Timer kick: skip if protection active, or if not relevant to current mode
            if not dry_run and tracker.consecutive_fail >= 1:
                if protected:
                    log(f"  SKIP kick {timer['name']} — protection active ({_test_active})")
                elif mode == "day" and timer["name"] in night_timers:
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


def _run_memory_check(results: dict, dry_run: bool = False):
    """메모리/swap 체크 — critical시 OOM recovery."""
    mem_ok, mem_info = check_memory()
    mem_tracker = _state.get("system:memory")
    if mem_ok:
        mem_tracker.record_success()
    else:
        if mem_tracker.record_failure() and mem_tracker.can_alert():
            send_alert("system:memory", mem_tracker.state.value,
                       f"mem={mem_info['pct']}% swap={mem_info['swap_pct']}%")
            _state.add_event("system:memory", "crit", f"{mem_info['pct']}%/{mem_info['swap_pct']}%")
            if not dry_run:
                recover_oom()

    # Trend recording for predictive monitoring
    _state.mem_trend.add(mem_info.get("pct", 0))
    results["memory"] = mem_info
    # Disk trend (cheapest reliable source: df output)
    try:
        disk_info = check_disk()
        root_disk = next((d for d in disk_info if d.get("mount") == "/"), {})
        _state.disk_trend.add(root_disk.get("pct", 0))
        results["disk_trend"] = {
            "root_pct": root_disk.get("pct", 0),
            "eta_disk_full": _state.disk_trend.predict_eta(97),
            "eta_disk_crit": _state.disk_trend.predict_eta(92),
        }
    except Exception:
        results["disk_trend"] = {}
    # Pipeline state stuck detection
    stuck = _state.check_pipeline_stuck()
    if stuck:
        for s in stuck:
            _state.add_event("pipeline_state", "stuck",
                             f"{s['state']}: {s['cnt']} turns, {s['stuck_sec']}s")
    results["pipeline_stuck"] = stuck


def _run_alert_only(dry_run: bool, results: dict):
    """Alert-only 서비스 체크 (복구 없음)."""
    for name in ALERT_ONLY_TARGETS:
        ok, detail = check_service(name)
        tracker = _state.get(f"svc:{name}")
        if ok:
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
                if not _test_active:
                    send_alert(f"svc:{name}", tracker.state.value, detail)
                    _state.add_event(f"svc:{name}", "down", detail)
        results.setdefault("services", []).append({"name": name, "ok": ok, "detail": detail})


def _run_common_checks(results: dict, dry_run: bool, mode: str = "day"):
    """모드 공통 체크 — 서비스, 타이머, 메모리, alert-only."""
    _run_services(results, dry_run)
    _run_timers(results, dry_run, mode)
    _run_memory_check(results, dry_run)
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
                # Test running → transient failures during mode switch are expected
                if not _test_active:
                    detail = f"T1={probe['t1_detail']} T2={probe['t2_detail']}"
                    send_alert(f"llm:{name}", tracker.state.value, detail)
                    _state.add_event(f"llm:{name}", "state_change", detail)

        lat_ok, lat_detail = check_probe_latency(probe["port"])
        if not lat_ok and lat_detail != "skip":
            _state.add_event(f"llm:{name}", "latency_warn", lat_detail)
            if not _test_active and tracker.can_alert():
                send_alert(f"llm:{name}", "LATENCY", lat_detail)

        results["probes"].append(probe)

    pipe_name, _ = check_pipeline("day_cycle.sh")
    if not pipe_name and not _test_active:
        try:
            work = psql_json(
                "SELECT count(*)::int AS cnt FROM turns "
                "WHERE pipeline_state NOT IN ('verified', 'pending') "
                "AND text != ''", timeout=5)
            in_flight = (work or [{}])[0].get("cnt", 0) if work else 0
            if in_flight > 0:
                log(f"  day_cycle.sh not running, {in_flight} in-flight — resuming")
                _state.add_event("day_cycle", "resume", f"{in_flight} in-flight")
                subprocess.run(["systemctl", "--user", "start", "devforge-day-cycle.service"],
                               capture_output=True, timeout=30)
            else:
                pending_work = psql_json(
                    "SELECT count(*)::int AS cnt FROM turns "
                    "WHERE pipeline_state = 'pending' "
                    "AND text != ''", timeout=5)
                pending_cnt = (pending_work or [{}])[0].get("cnt", 0) if pending_work else 0
                if pending_cnt > 0:
                    log(f"  day_cycle.sh not running, {pending_cnt} pending — starting first batch")
                    _state.add_event("day_cycle", "start", f"{pending_cnt} pending")
                    subprocess.run(["systemctl", "--user", "start", "devforge-day-cycle.service"],
                                   capture_output=True, timeout=30)
        except Exception as e:
            log(f"  day_cycle check error: {e}")
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
                if not _test_active:
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
                if not _test_active:
                    send_alert(f"pipeline:{phase_name}", "STOPPED", f"no process found")
                    _state.add_event(f"pipeline:{phase_name}", "stopped", "")
        results["pipeline_running"] = results["pipeline_running"] or running

    _run_common_checks(results, dry_run, "night")
    return results


# ── Active Pulse Query ──────────────────────────────────────

def _get_active_pulses() -> list[dict]:
    """Query IN_PROGRESS heartbeat pulses from watchdog_pulses."""
    try:
        rows = psql_json(
            "SELECT pulse_id, instruction, priority, status, "
            "EXTRACT(EPOCH FROM (now() - created_at))::int AS age_sec "
            "FROM watchdog_pulses "
            "WHERE pulse_id LIKE 'heartbeat_%' AND status = 'IN_PROGRESS' "
            "ORDER BY created_at DESC"
        )
        return rows or []
    except Exception:
        return []


def _get_active_test_pulses() -> list[dict]:
    """Query IN_PROGRESS test heartbeat pulses from watchdog_pulses."""
    try:
        rows = psql_json(
            "SELECT pulse_id, instruction, priority, status, "
            "EXTRACT(EPOCH FROM (now() - created_at))::int AS age_sec "
            "FROM watchdog_pulses "
            "WHERE pulse_id LIKE 'heartbeat_test_%' AND status = 'IN_PROGRESS' "
            "ORDER BY created_at DESC"
        )
        return rows or []
    except Exception:
        return []


def _get_test_db_progress() -> dict:
    """Query DB for recent pipeline activity (last 30min)."""
    try:
        emb = psql_json(
            "SELECT count(*) AS cnt FROM embeddings "
            "WHERE created_at > now() - interval '30 minutes'"
        ) or [{"cnt": 0}]
        facts = psql_json(
            "SELECT fact_type, count(*) AS cnt FROM review_facts "
            "WHERE created_at > now() - interval '30 minutes' "
            "GROUP BY fact_type ORDER BY fact_type"
        ) or []
        marks = psql_json(
            "SELECT count(*) AS cnt FROM review_facts "
            "WHERE fact_type = 'marker' "
            "AND created_at > now() - interval '30 minutes'"
        ) or [{"cnt": 0}]
        return {
            "embeddings_30m": emb[0]["cnt"] if emb else 0,
            "facts_30m": {r["fact_type"]: r["cnt"] for r in facts},
            "markers_30m": marks[0]["cnt"] if marks else 0,
        }
    except Exception:
        return {}


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
            slot_data = check_llm_slots(port)
            slots[str(port)] = slot_data
            # Feed slot state into stuck detector
            _state.update_slots(str(port), slot_data)
            # Feed aggregate token metrics into stagnation detector
            _state.update_token_metrics(str(port), metrics[str(port)])
        except Exception:
            pass

    # Check for stuck slots (processing but no progress)
    slots_stuck = _state.check_slots_stuck()
    if slots_stuck:
        for ss in slots_stuck:
            _state.add_event("slot_stuck", "deadlock",
                             f":{ss['port']} slots[{ss['slots']}] all stuck {ss['min_stuck_checks']} checks")
            log(f"  [slot-deadlock] :{ss['port']} slots[{ss['slots']}] — "
                f"deadlock detected ({ss['min_stuck_checks']} checks)")

    # Detect test active → collect progress from DB
    test_pulses = _get_active_test_pulses()
    test_progress = None
    if test_pulses:
        try:
            test_progress = {
                "pulses": test_pulses,
                "db": _get_test_db_progress(),
            }
        except Exception:
            pass

    return {
        "mode": mode,
        "experiment_active": experiment_active,
        "test_progress": test_progress,
        "containers": containers,
        "services": services,
        "timers": timers,
        "memory": mem,
        "probes": day_results.get("probes", []),
        "metrics": metrics,
        "slots": slots,
        "active_pulses": _get_active_pulses(),
        "events_30m": _state.events_since(1800),
        "disk_trend": day_results.get("disk_trend", {}),
        "pipeline_stuck": day_results.get("pipeline_stuck", []),
        "slots_stuck": slots_stuck,
    }


# ── Fix Loops ──────────────────────────────────────────────────────

def _fix_loop_common(pipe: str, llm_port: int):
    """Run fix loop for a pipeline that failed consecutively."""
    if _test_active:
        log(f"  SKIP fix loop for {pipe} — protection active ({_test_active})")
        return

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
    """Day mode: fix loop for day_cycle.sh failures (Pod A :8080)."""
    if _test_active:
        log(f"  SKIP day fix loop — protection active ({_test_active})")
        return
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)


def night_fix_loop():
    """Night mode: fix loop for night pipeline failures (Pod B :8081)."""
    if _test_active:
        log(f"  SKIP night fix loop — protection active ({_test_active})")
        return
    for pipe in ("night_cycle", "review_consumer", "proxy_reviewer"):
        _fix_loop_common(pipe, llm_port=8081)


# ── Main Loop ───────────────────────────────────────────────────────

def _check_slot_deadlocks(results: dict, dry_run: bool = False):
    """Detect and recover from slot deadlocks every cycle.

    llama-server --parallel N + --cache-reuse can cause all processing slots
    to livelock (tokens don't progress). We detect this by tracking
    slot state: if ALL processing slots on a port make no progress for
    SLOT_STUCK_THRESHOLD consecutive cycles → restart Pod B.
    """
    if dry_run:
        return
    if _test_active:
        log("  [slot-deadlock] SKIP — test active")
        return
    if is_experiment_active():
        log("  [slot-deadlock] SKIP — experiment active")
        return

    for probe in results.get("probes", []):
        port = str(probe["port"])
        try:
            slot_data = check_llm_slots(probe["port"])
            _state.update_slots(port, slot_data)
        except Exception:
            continue

    stuck_ports = _state.check_slots_stuck()
    for sp in stuck_ports:
        _state.add_event("slot_deadlock", "detected",
                         f":{sp['port']} slots[{sp['slots']}] stuck {sp['min_stuck_checks']} checks")
        log(f"  [slot-deadlock] :{sp['port']} slots[{sp['slots']}] — "
            f"deadlock confirmed, recovering...")
        ok = recover_slot_deadlock(sp["port"])
        if ok:
            _state.add_event("slot_deadlock", "recovered", f":{sp['port']} restarted")
        else:
            _state.add_event("slot_deadlock", "recovery_failed", f":{sp['port']}")
            send_alert("slot_deadlock", "DOWN",
                       f":{sp['port']} deadlock recovery failed")


def _recover_intermediate_states(results: dict, dry_run: bool = False):
    """Recover stale intermediate pipeline states every cycle.

    detecting → scanned, enriching → extracted, verifying → enriched.
    Tracked via ComponentTracker to prevent alert spam (circuit breaker).
    """
    if dry_run:
        return
    if _test_active:
        return

    from lib.db import psql_ok

    stuck = _state.check_intermediate_stuck()
    for s in stuck:
        state = s["state"]
        to_state = s["to_state"]
        cnt = s["cnt"]

        tracker = _state.get(f"pipeline_int:{state}")
        if tracker.consecutive_fail >= 3 and not tracker.can_retry():
            log(f"  SKIP {state} recovery — circuit open ({tracker.consecutive_fail} fails)")
            continue

        log(f"  [pipeline-stuck] {state}: {cnt} turns stale ≥{s['stale_sec']}s → reset to {to_state}")
        try:
            ok = psql_ok(
                f"UPDATE turns SET pipeline_state = '{to_state}' "
                f"WHERE pipeline_state = '{state}' "
                f"AND created_at < now() - interval '{s['stale_sec']} seconds'",
                timeout=10,
            )
        except Exception as e:
            log(f"  {state} recovery SQL failed: {e}")
            tracker.record_failure()
            continue

        if ok:
            _state.add_event(f"pipeline_stuck:{state}", "recovered",
                             f"{cnt} turns → {to_state}")
            tracker.record_success()
        else:
            tracker.record_failure()
            _state.add_event(f"pipeline_stuck:{state}", "recovery_failed",
                             f"{cnt} turns stuck")


def _check_token_stagnation(results: dict, dry_run: bool = False):
    """Detect aggregate token stagnation across all LLM ports.

    If a port shows processing > 0 in /metrics but total_prompt+total_gen
    doesn't advance for TOKEN_STAGNATION_THRESHOLD cycles → restart Pod B.
    Complements slot-level deadlock detection (catches task_id cycling).
    """
    if dry_run:
        return
    if _test_active:
        return
    if is_experiment_active():
        return

    # Feed metrics for probes that weren't covered in heartbeat summary
    for probe in results.get("probes", []):
        port = str(probe["port"])
        try:
            metrics = check_llm_metrics(probe["port"])
            _state.update_token_metrics(port, metrics)
        except Exception:
            continue

    stagnated = _state.check_token_stagnation()
    for st in stagnated:
        _state.add_event("token_stagnation", "detected",
                         f":{st['port']} tokens stuck {st['stagnation_count']} checks")
        log(f"  [token-stagnation] :{st['port']} — "
            f"aggregate tokens not advancing ({st['stagnation_count']} checks), recovering...")
        ok = recover_slot_deadlock(st["port"])
        if ok:
            _state.add_event("token_stagnation", "recovered", f":{st['port']} restarted")
            # Reset stagnation counter after recovery
            _state._token_stagnation[st["port"]] = {
                "total_prev": 0, "processing_prev": 0, "stagnation_count": 0,
            }
        else:
            _state.add_event("token_stagnation", "recovery_failed", f":{st['port']}")
            send_alert("token_stagnation", "DOWN",
                       f":{st['port']} token stagnation recovery failed")


def main_loop(one_shot: bool = False, dry_run: bool = False):
    global _running, _test_active

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

        # Dead man's switch — update liveness timestamp every cycle
        _state.update_liveness()

        # ONE check per cycle: is a test running?
        # All sub-functions use the module-level _test_active instead of
        # querying watchdog_pulses individually.
        test_pulses = _get_active_test_pulses()
        _test_active = bool(test_pulses)
        if _test_active:
            pulse_ids = [p["pulse_id"] for p in test_pulses]
            log(f"  Test active ({pulse_ids}) — alerts suppressed, fix loops skipped")

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

        # Heartbeat stale check — detect + auto-resolve
        stale_beats = check_heartbeats()
        for sb in stale_beats:
            log(f"  HEARTBEAT STALE: {sb['worker']} — last beat {sb['age_sec']} ago")
            _state.add_event("heartbeat", f"stale:{sb['worker']}",
                             f"age={sb['age_sec']} last={sb['last_beat']}")
            resolve_pulse(f"heartbeat_{sb['worker']}")
            log(f"  Auto-resolved stale pulse heartbeat_{sb['worker']}")

        if _state.should_heartbeat(HEARTBEAT_INTERVAL):
            try:
                summary = build_heartbeat_summary(results)
                heartbeat(summary)
            except Exception as e:
                log(f"heartbeat error: {e}")

        # Slot deadlock detection & recovery (every cycle)
        _check_slot_deadlocks(results, dry_run=dry_run)

        # Intermediate pipeline state recovery (every cycle)
        _recover_intermediate_states(results, dry_run=dry_run)

        # Token stagnation detection — aggregate token counter check (every cycle)
        _check_token_stagnation(results, dry_run=dry_run)

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
