# Status: production
# Path: imported by — watchdog.py (main_loop)
"""Watchdog orchestration — check runners, fix loops, action consumer, code scan.

Functions moved from __init__.py to keep __init__.py as re-export only.
All submodule imports here — no circular dep risk since __init__.py no
longer imports from orchestrator.
"""

import importlib
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from lib.action_queue import action_claim_pending, action_complete, action_fail, execute_action
from lib.db import psql_json
from lib.experiment_state import (
    cleanup_stale,
    is_experiment_active,
    is_experiment_stale,
)
from lib.experiment_state import (
    read_state as read_experiment_state,
)
from lib.experiment_state import (
    update_state as update_exp_state,
)
from lib.infra.health_checks import svc_active
from lib.watchdog.checker import (
    check_all_llm,
    check_all_oneshot_results,
    check_all_services,
    check_all_system_services,
    check_all_timers,
    check_disk,
    check_heartbeats,
    check_inference_container,  # noqa: F401 — used in day_fix_loop
    check_llm_metrics,
    check_llm_probe,  # noqa: F401 — used in day_fix_loop
    check_llm_slots,
    check_memory,
    check_pipeline,
    check_port_conflict,  # noqa: F401 — used in day_fix_loop
    check_probe_latency,
    check_service,
    read_mode,
)
from lib.watchdog.config import (
    ALERT_ONLY_TARGETS,
    CHECK_INTERVAL,
    HEARTBEAT_INTERVAL,
    LIVENESS_STALE_SEC,
    STALE_HEARTBEAT_KICKS,
    STALE_KICK_COOLDOWN_SEC,
    WATCHDOG_LIVENESS_FILE,
)
from lib.watchdog.messenger import get_undelivered, resolve_pulse
from lib.watchdog.notifier import heartbeat, send_alert, send_recovery, sd_notify
from lib.watchdog import incidents
from lib.watchdog.recovery import (
    graduated_recover,
    kill_stale_process,
    recover_ebook_watcher,
    recover_inference_cascade,  # noqa: F401 — used in day_fix_loop
    recover_oom,
    recover_oneshot,
    recover_port_conflict,  # noqa: F401 — used in day_fix_loop
    recover_service,
    recover_slot_deadlock,
)

from ._globals import CODE_SCAN_INTERVAL as _CODE_SCAN_INTERVAL
from ._globals import _code_scan_counter, _running, _start_time, _state, _test_active

# Last kick timestamp per stale-heartbeat worker (monotonic). Guards the
# one-shot self-heal kick so it never becomes a per-cycle restart storm.
_stale_kick_ts: dict[str, float] = {}


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
    _state.set_mode(read_mode())
    log(f"config reloaded, mode={_state.mode}")


def sigusr1_handler(signum, frame):
    """SIGUSR1 — dump current state to journal."""
    log("=== Watchdog status dump ===")
    log(f"  mode={_state.mode}, running={_running}, uptime={int(time.monotonic() - _start_time)}s")
    for s in _state.all_summaries():
        log(
            f"  {s['name']:25s} state={s['state']:10s} fails={s['fail_count']} consecutive={s['consecutive_fail']} circuit={s['circuit_open']}"
        )
    log(f"  events_in_buffer={len(_state._events)}")
    log("==============================")


# ── Common checks ──────────────────────────────────────────────────────


def _run_services(results: dict, dry_run: bool):
    global _test_active
    for svc in check_all_services():
        name = svc["name"]
        tracker = _state.get(f"svc:{name}")
        if svc["ok"]:
            tracker.record_success()
            incidents.resolve_if_open(f"svc:{name}")
        elif not dry_run and not is_experiment_active():
            inc_id = incidents.record_detect(f"svc:{name}", "down", svc["detail"], unit=name)
            if name == "ebook-watcher":
                # ebook-watcher: restart 후 readiness(프로세스+로그활동)까지 확인
                ok = graduated_recover(name, tracker, recover_ebook_watcher)
            else:
                ok = graduated_recover(name, tracker, lambda n=name: recover_service(n))
            incidents.record_action(inc_id, "restart", bool(ok))
            if not _test_active and tracker.is_degraded() and tracker.can_alert():
                send_alert(f"svc:{name}", tracker.state.value, svc["detail"])
                _state.add_event(f"svc:{name}", "down", svc["detail"])
        else:
            incidents.record_detect(f"svc:{name}", "down", svc["detail"], unit=name)
            if tracker.record_failure() and tracker.can_alert():
                if not _test_active:
                    send_alert(f"svc:{name}", tracker.state.value, svc["detail"])
                    _state.add_event(f"svc:{name}", "down", svc["detail"])
        results["services"].append(svc)


def _run_timers(results: dict, dry_run: bool, mode: str = "day"):
    for timer in check_all_timers():
        name = timer["name"]
        tracker = _state.get(f"timer:{name}")
        if timer["ok"]:
            tracker.record_success()
            incidents.resolve_if_open(f"timer:{name}")
        else:
            protected = _test_active
            svc_name = name.replace(".timer", ".service")
            inc_id = incidents.record_detect(f"timer:{name}", "delay", timer["detail"], unit=svc_name)
            if not protected:
                if tracker.record_failure() and tracker.can_alert():
                    send_alert(f"timer:{name}", "DELAY", timer["detail"])
                    _state.add_event(f"timer:{name}", "delay", timer["detail"])
            if not dry_run and tracker.consecutive_fail >= 1:
                if protected:
                    log(f"  SKIP kick {name} — protection active ({_test_active})")
                else:
                    log(f"  kicking {svc_name} (timer delayed {timer['detail']})")
                    r = subprocess.run(
                        ["systemctl", "--user", "start", svc_name],
                        capture_output=True,
                        timeout=10,
                    )
                    incidents.record_action(inc_id, "kick", r.returncode == 0)
        results["timers"].append(timer)


def _run_memory_check(results: dict, dry_run: bool = False):
    mem_ok, mem_info = check_memory()
    mem_tracker = _state.get("system:memory")
    if mem_ok:
        mem_tracker.record_success()
    else:
        if mem_tracker.record_failure() and mem_tracker.can_alert():
            send_alert(
                "system:memory",
                mem_tracker.state.value,
                f"mem={mem_info['pct']}% swap={mem_info['swap_pct']}%",
            )
            _state.add_event("system:memory", "crit", f"{mem_info['pct']}%/{mem_info['swap_pct']}%")
            if not dry_run:
                recover_oom()
    _state.mem_trend.add(mem_info.get("pct", 0))
    results["memory"] = mem_info
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
    stuck = _state.check_pipeline_stuck()
    if stuck:
        for s in stuck:
            _state.add_event(
                "pipeline_state", "stuck", f"{s['state']}: {s['cnt']} turns, {s['stuck_sec']}s"
            )
    results["pipeline_stuck"] = stuck


def _run_alert_only(dry_run: bool, results: dict):
    for name in ALERT_ONLY_TARGETS:
        ok, detail = check_service(name)
        tracker = _state.get(f"svc:{name}")
        if ok:
            tracker.record_success()
            incidents.resolve_if_open(f"svc:{name}")
        else:
            incidents.record_detect(f"svc:{name}", "down", detail, unit=name)
            if tracker.record_failure() and tracker.can_alert():
                if not _test_active:
                    send_alert(f"svc:{name}", tracker.state.value, detail)
                    _state.add_event(f"svc:{name}", "down", detail)
        results.setdefault("services", []).append({"name": name, "ok": ok, "detail": detail})


def _run_oneshot_results(dry_run: bool, results: dict):
    """One-shot 서비스 self-heal — 실패 시 재실행(backoff+circuit), 반복 실패 시 알림."""
    for item in check_all_oneshot_results():
        name = item["name"]
        tracker = _state.get(f"oneshot:{name}")
        if item["ok"]:
            tracker.record_success()
            incidents.resolve_if_open(f"oneshot:{name}")
        elif not dry_run and not is_experiment_active():
            inc_id = incidents.record_detect(f"oneshot:{name}", "failed", item["detail"], unit=name)
            log(f"  oneshot {name} failed ({item['detail']}) — self-heal (re-run)")
            ok = graduated_recover(name, tracker, lambda n=name: recover_oneshot(n))
            incidents.record_action(inc_id, "re-run", bool(ok))
            if not _test_active and tracker.is_degraded() and tracker.can_alert():
                send_alert(f"oneshot:{name}", tracker.state.value, item["detail"])
                _state.add_event(f"oneshot:{name}", "failed", item["detail"])
        else:
            if tracker.record_failure() and tracker.can_alert():
                if not _test_active:
                    send_alert(f"oneshot:{name}", tracker.state.value, item["detail"])
                    _state.add_event(f"oneshot:{name}", "failed", item["detail"])
        results.setdefault("services", []).append(item)


def _run_system_services(dry_run: bool, results: dict):
    """system 스코프(rootful) 서비스 — alert-only (caddy/netdata)."""
    for item in check_all_system_services():
        name = item["name"]
        tracker = _state.get(f"syssvc:{name}")
        if item["ok"]:
            tracker.record_success()
            incidents.resolve_if_open(f"syssvc:{name}")
        else:
            incidents.record_detect(f"syssvc:{name}", "down", item["detail"], unit=f"system:{name}")
            if tracker.record_failure() and tracker.can_alert():
                if not _test_active:
                    send_alert(f"syssvc:{name}", tracker.state.value, item["detail"])
                    _state.add_event(f"syssvc:{name}", "down", item["detail"])
        results.setdefault("services", []).append(item)


_last_prune_ts = 0.0


def _maybe_prune() -> None:
    """Retention 회전 — 하루 1회만 실행."""
    global _last_prune_ts
    if time.time() - _last_prune_ts < 86400:
        return
    try:
        incidents.prune()
        _last_prune_ts = time.time()
    except Exception:
        pass


def _write_liveness() -> None:
    """watchdog 자체 생존 신호 기록 (외부/타이머가 검사)."""
    try:
        with open(WATCHDOG_LIVENESS_FILE, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def _ping_external() -> None:
    """외부 dead-man's switch 핑. HTTP(WATCHDOG_PING_URL) 또는 SSH(WATCHDOG_PING_SSH)."""
    global _last_ssh_ping
    url = os.environ.get("WATCHDOG_PING_URL", "")
    if url:
        try:
            import urllib.request

            urllib.request.urlopen(url, timeout=5).read()
        except Exception:
            pass
    host = os.environ.get("WATCHDOG_PING_SSH", "")
    if host and time.time() - _last_ssh_ping >= 300:  # 5분에 1회만(SSH 부하 제한)
        try:
            subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                 "python3 ~/wd_monitor/wdpulse.py record"],
                capture_output=True, timeout=20,
            )
            _last_ssh_ping = time.time()
        except Exception:
            pass


_last_ssh_ping = 0.0


def _run_common_checks(results: dict, dry_run: bool, mode: str = "day"):
    _run_services(results, dry_run)
    _run_timers(results, dry_run, mode)
    _run_memory_check(results, dry_run)
    _run_alert_only(dry_run, results)
    _run_oneshot_results(dry_run, results)
    _run_system_services(dry_run, results)
    _maybe_prune()


# ── Day checks ──────────────────────────────────────────────────────


def run_day_checks(dry_run: bool = False) -> dict:
    results = {
        "containers": [],
        "services": [],
        "timers": [],
        "probes": [],
        "memory": {},
        "pipeline_running": False,
    }
    for probe in check_all_llm():
        name = probe["name"]
        tracker = _state.get(f"llm:{name}")
        ok = probe["t1_ok"] and probe["t2_ok"]
        if ok:
            tracker.record_success()
        else:
            if tracker.record_failure() and tracker.can_alert():
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
                "WHERE pipeline_state NOT IN ('embedded', 'embed_skipped', 'pending') "
                "AND text != ''",
                timeout=5,
            )
            in_flight = (work or [{}])[0].get("cnt", 0) if work else 0
            if in_flight > 0:
                log(f"  day_cycle.sh not running, {in_flight} in-flight — resuming")
                _state.add_event("day_cycle", "resume", f"{in_flight} in-flight")
                subprocess.run(
                    ["systemctl", "--user", "--no-block", "start", "devforge-day-cycle.service"],
                    capture_output=True,
                    timeout=10,
                )
            else:
                pending_work = psql_json(
                    "SELECT count(*)::int AS cnt FROM turns "
                    "WHERE pipeline_state = 'pending' "
                    "AND text != ''",
                    timeout=5,
                )
                pending_cnt = (pending_work or [{}])[0].get("cnt", 0) if pending_work else 0
                if pending_cnt > 0:
                    log(f"  day_cycle.sh not running, {pending_cnt} pending — starting first batch")
                    _state.add_event("day_cycle", "start", f"{pending_cnt} pending")
                    subprocess.run(
                        [
                            "systemctl",
                            "--user",
                            "--no-block",
                            "start",
                            "devforge-day-cycle.service",
                        ],
                        capture_output=True,
                        timeout=10,
                    )
        except Exception as e:
            log(f"  day_cycle check error: {e}")
    results["pipeline_running"] = pipe_name
    _run_common_checks(results, dry_run, "day")
    return results


# ── Active Pulse Query ──────────────────────────────────────


def _get_active_pulses() -> list:
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


def _get_active_test_pulses() -> list:
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
    try:
        emb = psql_json(
            "SELECT count(*) AS cnt FROM embeddings "
            "WHERE created_at > now() - interval '30 minutes'"
        ) or [{"cnt": 0}]
        facts = (
            psql_json(
                "SELECT fact_type, count(*) AS cnt FROM review_facts "
                "WHERE created_at > now() - interval '30 minutes' "
                "GROUP BY fact_type ORDER BY fact_type"
            )
            or []
        )
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


# ── Code Quality Scanner ──────────────────────────────────────


def _code_quality_scan():
    from lib.watchdog import codescanner
    from lib.watchdog.fixloop import run_fix_loop

    findings = codescanner.run_scan()
    if not findings:
        return
    for finding in findings:
        error_log = (
            f"Code quality issue in {finding['file']}:{finding['line']}\n"
            f"Pattern: {finding['label']}\n"
            f"Matched:\n{finding['matched']}\n\n"
            f"Context:\n{finding['context']}"
        )
        context = (
            f"This is a batch LLM pipeline file under "
            f"/opt/projects/server/scripts/pipelines/{finding['file']}.\n"
            f"Fix by converting 'except Exception:' to "
            f"'except Exception as e:' and adding a "
            f"print(f'  [{{component}}] {{e}}', flush=True) line with "
            f"the appropriate component label based on context."
        )
        result = run_fix_loop(error_log, context, llm_port=8082, max_attempts=2)
        if result["fixed"]:
            codescanner.mark_fixed(finding)
            log(
                f"  [code-scan] auto-fixed {finding['file']}:{finding['line']} "
                f"({result['detail'][:60]})"
            )
        else:
            ft = result.get("failure_type", "unknown")
            fp = result.get("failure_phase", "")
            codescanner.mark_failed(finding, f"[{fp}/{ft}] {result.get('detail', '')[:180]}")
            log(
                f"  [code-scan] FAILED {finding['file']}:{finding['line']} "
                f"— {result['failure_phase']}/{result['failure_type']}: {result['detail'][:60]}"
            )


# ── Fix Loops ────────────────────────────────────────────────────


def _fix_loop_common(pipe: str, llm_port: int):
    if _test_active:
        log(f"  SKIP fix loop for {pipe} — protection active ({_test_active})")
        return
    from lib.watchdog.fixloop import run_fix_loop

    tracker = _state.get(f"pipeline:{pipe}")
    if tracker.consecutive_fail >= 2 and tracker.can_retry():
        log(f"  {pipe} failed {tracker.consecutive_fail}x, launching fix loop ({llm_port})")
        exp = read_experiment_state()
        if exp and exp.get("error_log"):
            error_log = exp["error_log"]
        else:
            error_log = f"pipeline:{pipe} failed {tracker.consecutive_fail} consecutive times"
        result = run_fix_loop(error_log, f"pipeline/{pipe}", llm_port=llm_port)
        if result["fixed"]:
            _state.add_event(f"fix:{pipe}", "fixed", result["detail"])
            send_recovery(f"fix:{pipe}", result["detail"])
            if exp:
                update_exp_state(error_log=None, fix_attempts=0)
        else:
            _state.add_event(f"fix:{pipe}", "failed", result["detail"])
            if exp:
                update_exp_state(fix_attempts=exp.get("fix_attempts", 0) + 1)


def day_fix_loop():
    """3-Phase day recovery: infra → pipeline-stuck → LLM code fix.

    Phase 1 — Infra diagnosis (returns on first broken layer):
      1a. Port conflict in day_cycle journal → recover_port_conflict()
      1b. Inference container stuck (Created/Exited) → recover_inference_cascade()
      1c. LLM probe :8082 fails → recover_inference_cascade()

    Phase 2 — Pipeline state stuck (3600s no change) → restart day_cycle.

    Phase 3 — Only if infra+state healthy → LLM code fix loop (existing).
    """
    if _test_active:
        log(f"  SKIP day fix loop — protection active ({_test_active})")
        return

    # ── Phase 1a: port conflict ──
    port_ok, port_detail = check_port_conflict()
    if not port_ok:
        log(f"  [watchdog] {port_detail}")
        _state.add_event("infra", "port_conflict", port_detail)
        if recover_port_conflict():
            _state.add_event("infra", "port_conflict_recovered", "inference restarted")
            send_recovery("infra:port_conflict", "inference restarted after port conflict")
            return
        _state.add_event("infra", "port_conflict_failed", "all recovery levels failed")
        send_alert("infra:port_conflict", "DOWN", "cascade recovery failed")
        return

    # ── Phase 1b: inference container state ──
    infer_ok, infer_detail = check_inference_container()
    if not infer_ok:
        log(f"  [watchdog] inference container issue: {infer_detail}")
        _state.add_event("infra", "inference_down", infer_detail)
        if recover_inference_cascade():
            _state.add_event("infra", "inference_recovered", "cascade OK")
            send_recovery("infra:inference", "inference container restarted")
            return
        _state.add_event("infra", "inference_failed", "cascade recovery failed")
        send_alert("infra:inference", "DOWN", "cascade recovery failed")
        return

    # ── Phase 1c: LLM probe on :8082 (day-extractor) ──
    probe_ok, probe_detail = check_llm_probe(8082, "day-extract")
    if not probe_ok:
        log(f"  [watchdog] LLM probe :8082 failed: {probe_detail}")
        _state.add_event("infra", "llm_probe_failed", probe_detail)
        if recover_inference_cascade():
            _state.add_event("infra", "llm_probe_recovered", "inference restarted")
            send_recovery("infra:llm_probe", "inference restarted after probe failure")
        return

    # ── Phase 2: pipeline state stuck ──
    stuck = _state.check_pipeline_stuck()
    if stuck:
        for s in stuck:
            log(f"  [watchdog] pipeline stuck: {s['state']} ({s['cnt']} turns, {s['stuck_sec']}s)")
            _state.add_event("pipeline_stuck", s["state"], f"{s['cnt']} turns, {s['stuck_sec']}s")
        log("  [watchdog] restarting day_cycle to unstick pipeline")
        subprocess.run(
            ["systemctl", "--user", "restart", "devforge-day-cycle.service"],
            capture_output=True,
            timeout=30,
        )
        return

    # ── Phase 3: LLM code fix (only when infra + state healthy) ──
    for pipe in ("day_cycle",):
        _fix_loop_common(pipe, llm_port=8082)


# ── Slot deadlock / Token stagnation / Intermediate recovery ────────────


def _check_slot_deadlocks(results: dict, dry_run: bool = False):
    if dry_run or _test_active or is_experiment_active():
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
        _state.add_event(
            "slot_deadlock",
            "detected",
            f":{sp['port']} slots[{sp['slots']}] stuck {sp['min_stuck_checks']} checks",
        )
        log(
            f"  [slot-deadlock] :{sp['port']} slots[{sp['slots']}] — deadlock confirmed, recovering..."
        )
        ok = recover_slot_deadlock(sp["port"])
        if ok:
            _state.add_event("slot_deadlock", "recovered", f":{sp['port']} restarted")
        else:
            _state.add_event("slot_deadlock", "recovery_failed", f":{sp['port']}")
            send_alert("slot_deadlock", "DOWN", f":{sp['port']} deadlock recovery failed")


def _recover_intermediate_states(results: dict, dry_run: bool = False):
    if dry_run or _test_active:
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
        log(
            f"  [pipeline-stuck] {state}: {cnt} turns stale ≥{s['stale_sec']}s → reset to {to_state}"
        )
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
            _state.add_event(f"pipeline_stuck:{state}", "recovered", f"{cnt} turns → {to_state}")
            tracker.record_success()
        else:
            tracker.record_failure()
            _state.add_event(f"pipeline_stuck:{state}", "recovery_failed", f"{cnt} turns stuck")


def _check_token_stagnation(results: dict, dry_run: bool = False):
    if dry_run or _test_active or is_experiment_active():
        return
    for probe in results.get("probes", []):
        port = str(probe["port"])
        try:
            metrics = check_llm_metrics(probe["port"])
            _state.update_token_metrics(port, metrics)
        except Exception:
            continue
    stagnated = _state.check_token_stagnation()
    for st in stagnated:
        _state.add_event(
            "token_stagnation",
            "detected",
            f":{st['port']} tokens stuck {st['stagnation_count']} checks",
        )
        log(
            f"  [token-stagnation] :{st['port']} — aggregate tokens not advancing ({st['stagnation_count']} checks), recovering..."
        )
        ok = recover_slot_deadlock(st["port"])
        if ok:
            _state.add_event("token_stagnation", "recovered", f":{st['port']} restarted")
            _state._token_stagnation[st["port"]] = {
                "total_prev": 0,
                "processing_prev": 0,
                "stagnation_count": 0,
            }
        else:
            _state.add_event("token_stagnation", "recovery_failed", f":{st['port']}")
            send_alert(
                "token_stagnation", "DOWN", f":{st['port']} token stagnation recovery failed"
            )


# ── Action Queue Consumer ──────────────────────────────────────


def _consume_actions(dry_run: bool = False):
    if dry_run or _test_active:
        return
    actions = action_claim_pending(max_count=10)
    if not actions:
        return
    log(f"  [action-queue] {len(actions)} actions claimed")
    for action in actions:
        pulse_id = action["pulse_id"]
        action_type = action["action_type"]
        instruction = action["instruction"]
        log(f"    executing {pulse_id}: {action_type} - {instruction[:80]}")
        try:
            ok, msg = execute_action(action)
        except Exception as e:
            ok, msg = False, f"execute_action raised: {e}"
        if ok:
            log(f"    {pulse_id}: OK - {msg[:100]}")
            action_complete(pulse_id, msg)
        else:
            log(f"    {pulse_id}: FAIL - {msg[:100]}")
            action_fail(pulse_id, msg)
            _state.add_event(f"action:{action_type}", "failed", f"{pulse_id}: {msg[:100]}")


# ── Heartbeat Summary ──────────────────────────────────────


def build_heartbeat_summary(results: dict) -> dict:
    """Build summary dict for 30min heartbeat."""
    mode = read_mode()
    experiment_active = is_experiment_active()
    containers = []
    for probe in results.get("probes", []):
        ok = probe["t1_ok"] and probe["t2_ok"]
        containers.append(
            {
                "name": probe["name"],
                "port": probe["port"],
                "mode": probe.get("name", "?"),
                "ok": ok,
                "uptime": probe.get("t2_detail", ""),
            }
        )
    services = []
    for svc in results.get("services", []):
        services.append({"name": svc["name"], "detail": "OK" if svc["ok"] else "DOWN"})
    timers = results.get("timers", [])
    mem = results.get("memory", {})
    if mem:
        mem["swap_used_gb"] = round(mem.get("swap_used_mb", 0) / 1024, 1)
        mem["swap_total_gb"] = round(mem.get("swap_total_mb", 0) / 1024, 1)
    metrics = {}
    slots = {}
    for probe in results.get("probes", []):
        port = probe["port"]
        try:
            metrics[str(port)] = check_llm_metrics(port)
            slot_data = check_llm_slots(port)
            slots[str(port)] = slot_data
            _state.update_slots(str(port), slot_data)
            _state.update_token_metrics(str(port), metrics[str(port)])
        except Exception:
            pass
    slots_stuck = _state.check_slots_stuck()
    if slots_stuck:
        for ss in slots_stuck:
            _state.add_event(
                "slot_stuck",
                "deadlock",
                f":{ss['port']} slots[{ss['slots']}] all stuck {ss['min_stuck_checks']} checks",
            )
            log(
                f"  [slot-deadlock] :{ss['port']} slots[{ss['slots']}] — deadlock detected ({ss['min_stuck_checks']} checks)"
            )
    test_pulses = _get_active_test_pulses()
    test_progress = None
    if test_pulses:
        try:
            test_progress = {"pulses": test_pulses, "db": _get_test_db_progress()}
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
        "probes": results.get("probes", []),
        "metrics": metrics,
        "slots": slots,
        "active_pulses": _get_active_pulses(),
        "events_30m": _state.events_since(1800),
        "disk_trend": results.get("disk_trend", {}),
        "pipeline_stuck": results.get("pipeline_stuck", []),
        "slots_stuck": slots_stuck,
    }


# ── Main Loop ───────────────────────────────────────────────────────


def _code_quality_scan_wrapper():
    """Periodic silent-catch pattern scan wrapper (tracks cycle counter)."""
    from lib.watchdog import codescanner
    from lib.watchdog.fixloop import run_fix_loop

    findings = codescanner.run_scan()
    if not findings:
        return
    for finding in findings:
        error_log = (
            f"Code quality issue in {finding['file']}:{finding['line']}\n"
            f"Pattern: {finding['label']}\n"
            f"Matched:\n{finding['matched']}\n\n"
            f"Context:\n{finding['context']}"
        )
        context = (
            f"This is a batch LLM pipeline file under "
            f"/opt/projects/server/scripts/pipelines/{finding['file']}.\n"
            f"Fix by converting 'except Exception:' to "
            f"'except Exception as e:' and adding a "
            f"print(f'  [{{component}}] {{e}}', flush=True) line with "
            f"the appropriate component label based on context."
        )
        result = run_fix_loop(error_log, context, llm_port=8082, max_attempts=2)
        if result["fixed"]:
            codescanner.mark_fixed(finding)
            log(
                f"  [code-scan] auto-fixed {finding['file']}:{finding['line']} ({result['detail'][:60]})"
            )
        else:
            ft = result.get("failure_type", "unknown")
            fp = result.get("failure_phase", "")
            codescanner.mark_failed(finding, f"[{fp}/{ft}] {result.get('detail', '')[:180]}")
            log(
                f"  [code-scan] FAILED {finding['file']}:{finding['line']} — {result['failure_phase']}/{result['failure_type']}: {result['detail'][:60]}"
            )


def _handle_stale_heartbeats(stale_beats: list) -> None:
    """Handle stale worker heartbeats.

    Default workers → log + event + auto-resolve (existing behavior).
    Workers in STALE_HEARTBEAT_KICKS (one-shot scheduled jobs like the news
    collector) are kept IN_PROGRESS so staleness stays detectable, and the
    mapped service is kicked at most once per STALE_KICK_COOLDOWN_SEC. This
    self-heals a run that fired but never completed, without a restart storm.
    """
    for sb in stale_beats:
        worker = sb["worker"]
        pulse = f"heartbeat_{worker}"
        log(f"  HEARTBEAT STALE: {worker} — last beat {sb['age_sec']} ago")

        svc = STALE_HEARTBEAT_KICKS.get(worker)
        if not svc:
            _state.add_event("heartbeat", f"stale:{worker}",
                             f"age={sb['age_sec']} last={sb['last_beat']}")
            resolve_pulse(pulse)
            log(f"  Auto-resolved stale pulse {pulse}")
            continue

        now = time.monotonic()
        if now - _stale_kick_ts.get(worker, 0.0) < STALE_KICK_COOLDOWN_SEC:
            log(f"  {worker} stale — kick throttled (cooldown {STALE_KICK_COOLDOWN_SEC}s)")
            continue

        _stale_kick_ts[worker] = now
        _state.add_event("heartbeat", f"stale:{worker}",
                         f"age={sb['age_sec']} last={sb['last_beat']}")
        if _test_active or is_experiment_active():
            log(f"  {worker} stale — protection active ({_test_active}), kick skipped")
            continue
        if svc_active(svc):
            log(f"  {svc} already active — kick skipped")
            continue
        log(f"  kicking {svc} (completion heartbeat stale)")
        subprocess.run(["systemctl", "--user", "--no-block", "start", svc],
                       capture_output=True, timeout=10)


def main_loop(one_shot: bool = False, dry_run: bool = False):
    signal.signal(signal.SIGTERM, sigterm_handler)
    signal.signal(signal.SIGINT, sigterm_handler)
    signal.signal(signal.SIGHUP, sighup_handler)
    signal.signal(signal.SIGUSR1, sigusr1_handler)

    kill_stale_process("watchdog.py")

    log(
        f"Watchdog started (interval={_state._check_interval if hasattr(_state, '_check_interval') else CHECK_INTERVAL}s, dry_run={dry_run})"
    )
    log(f"Initial mode: {read_mode()}")
    sd_notify("READY=1")

    # 영속화된 상태 복원 (재시작 후 backoff/circuit 보존)
    _state.load_state()
    n_restored = len(_state.all_summaries())
    if n_restored:
        log(f"Restored {n_restored} component states from persistence")

    _state.set_mode(read_mode())

    last_state_save = 0.0
    while _running:
        loop_start = time.monotonic()
        mode = read_mode()
        _state.set_mode(mode)
        _state.update_liveness()
        sd_notify("WATCHDOG=1")
        _write_liveness()
        _ping_external()

        test_pulses = _get_active_test_pulses()
        _test_active = bool(test_pulses)
        if _test_active:
            pulse_ids = [p["pulse_id"] for p in test_pulses]
            log(f"  Test active ({pulse_ids}) — alerts suppressed, fix loops skipped")

        experiment_active = is_experiment_active()
        if experiment_active:
            log("  Experiment detected — monitor-only mode (no recovery/fix loops)")

        if is_experiment_stale():
            log("  Stale experiment state detected — cleaning up")
            _state.add_event("experiment", "stale_cleanup", "")
            cleanup_stale()

        if mode == "day":
            msgs = get_undelivered("operator")
            for m in msgs:
                log(f"[TO_OPERATOR] {m['type']}: {m['content']}")

        try:
            results = run_day_checks(dry_run=dry_run)
            log("day check done")
            if not dry_run and not experiment_active:
                day_fix_loop()
        except Exception as e:
            log(f"Check cycle error: {e}")
            import traceback

            traceback.print_exc()

        stale_beats = check_heartbeats()
        _handle_stale_heartbeats(stale_beats)

        if _state.should_heartbeat(HEARTBEAT_INTERVAL):
            try:
                summary = build_heartbeat_summary(results)
                heartbeat(summary)
            except Exception as e:
                log(f"heartbeat error: {e}")

        _check_slot_deadlocks(results, dry_run=dry_run)
        _recover_intermediate_states(results, dry_run=dry_run)
        _check_token_stagnation(results, dry_run=dry_run)
        _consume_actions(dry_run=dry_run)

        global _code_scan_counter
        _code_scan_counter += 1
        if (
            _code_scan_counter >= _CODE_SCAN_INTERVAL
            and not dry_run
            and not experiment_active
            and not _test_active
        ):
            _code_scan_counter = 0
            _code_quality_scan_wrapper()

        if one_shot:
            break

        # 상태 주기적 영속화 (업계 표준: 재시작에도 backoff/circuit 유지)
        try:
            from lib.watchdog.config import STATE_SAVE_INTERVAL
            if time.monotonic() - last_state_save >= STATE_SAVE_INTERVAL:
                _state.save_state()
                last_state_save = time.monotonic()
        except Exception:
            pass

        elapsed = time.monotonic() - loop_start
        sleep_sec = max(1, CHECK_INTERVAL - int(elapsed))
        time.sleep(sleep_sec)

    if not one_shot:
        log("Watchdog stopped")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="DevForge Watchdog")
    parser.add_argument("--one-shot", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Check only, no recovery")
    parser.add_argument(
        "--liveness-check", action="store_true",
        help="Check watchdog liveness file; alert+exit1 if stale (timer)",
    )
    parser.add_argument(
        "--notify-failure", action="store_true", help="Send failure alert (OnFailure unit)",
    )
    args = parser.parse_args()

    if args.notify_failure:
        log("watchdog OnFailure: service entered failed state")
        try:
            send_alert("watchdog:failed", "DOWN", "watchdog service entered failed state (OnFailure)")
        except Exception as e:
            log(f"notify-failure error: {e}")
        return

    if args.liveness_check:
        try:
            age = time.time() - float(open(WATCHDOG_LIVENESS_FILE).read().strip())
        except Exception:
            age = None
        if age is None or age > LIVENESS_STALE_SEC:
            msg = f"watchdog liveness stale: {'missing' if age is None else str(int(age)) + 's'} (limit {LIVENESS_STALE_SEC}s)"
            log(msg)
            try:
                send_alert("watchdog:liveness", "DOWN", msg)
            except Exception:
                pass
            sys.exit(1)
        log(f"watchdog liveness OK ({int(age)}s ago)")
        return

    main_loop(one_shot=args.one_shot, dry_run=args.dry_run)
