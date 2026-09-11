# Status: production
# Path: imported by — cli.py (watch subcommand)
"""CLI watch commands — server status, alerts, pulse queue, event log."""

from lib.db import esc_sql, psql_json
from lib.watchdog.messenger import get_pulse, list_pulses, log_message, resolve_pulse


def cmd_watch_status(args):
    """서버 생존 + pulse 큐 + 이벤트 한눈에."""
    print("=" * 55)
    print("   WATCH STATUS")
    print("=" * 55)

    # Pulse summary
    pending = list_pulses("PENDING", 100)
    in_progress = list_pulses("IN_PROGRESS", 100)
    human = list_pulses("HUMAN_REQUIRED", 100)
    print(
        f"\n  Pulses: {len(pending)} pending | {len(in_progress)} in-progress | {len(human)} human-required"
    )
    for p in pending[:5]:
        fid = p.get("target_file", "")
        fstr = f" → {fid}" if fid else ""
        print(f"    [{p['priority']}] {p['instruction'][:60]}{fstr}")

    # Last 5 catchdog events
    rows = psql_json(
        "SELECT component, event_type, to_state, detail, created_at "
        "FROM catchdog_events ORDER BY created_at DESC LIMIT 5"
    )
    if rows:
        print("\n  Recent Events:")
        for r in rows:
            raw_ts = r.get("created_at")
            utc_timestamp = raw_ts[11:16] if isinstance(raw_ts, str) and len(raw_ts) >= 16 else "?"
            print(
                f"    [{utc_timestamp}] {r['component']}:{r['event_type']}"
                f"{' → ' + r['to_state'] if r.get('to_state') else ''}"
            )

    # Alerts
    alerts = psql_json(
        "SELECT component, event_type, detail, created_at "
        "FROM catchdog_events WHERE event_type IN ('down','delay','crit','fail','stopped') "
        "ORDER BY created_at DESC LIMIT 5"
    )
    if alerts:
        print("\n  Alerts:")
        for a in alerts:
            raw_ts = a.get("created_at")
            utc_timestamp = (
                raw_ts[5:16].replace("T", " ")
                if isinstance(raw_ts, str) and len(raw_ts) >= 16
                else "?"
            )
            print(
                f"    [{utc_timestamp}] {a['component']}: {a['event_type']} — {a.get('detail', '?')[:50]}"
            )

    print()


def cmd_watch_alerts(args):
    """PENDING + HUMAN_REQUIRED pulse 목록."""
    total = 0
    for status in ("PENDING", "HUMAN_REQUIRED"):
        pulses = list_pulses(status, 50)
        total += len(pulses)
        if pulses:
            print(f"\n── {status} ({len(pulses)}) ──")
            for p in pulses:
                print(f"  {p['pulse_id']}")
                print(f"    [{p['priority']}] {p['instruction'][:80]}")
                if p.get("target_file"):
                    print(f"    target: {p['target_file']}")
                print(f"    retry: {p.get('retry_count', 0)}/{p.get('max_retries', 3)}")

    db_alerts = psql_json(
        "SELECT component, event_type, detail, created_at "
        "FROM catchdog_events WHERE event_type IN ('down','fail','stopped') "
        "AND created_at > now() - interval '24 hours' "
        "ORDER BY created_at DESC LIMIT 10"
    )
    if db_alerts:
        print(f"\n── Server Alerts (24h, {len(db_alerts)}) ──")
        for a in db_alerts:
            raw_ts = a.get("created_at")
            utc_timestamp = (
                raw_ts[5:16].replace("T", " ")
                if isinstance(raw_ts, str) and len(raw_ts) >= 16
                else "?"
            )
            print(
                f"  [{utc_timestamp}] {a['component']}: {a['event_type']} — {a.get('detail', '?')[:60]}"
            )

    if total == 0 and not db_alerts:
        print("  No active alerts")
    print()


def cmd_watch_pulses_list(args):
    """PENDING pulses 목록."""
    pulses = list_pulses("PENDING", args.limit)
    if not pulses:
        print("  No PENDING pulses")
        return
    print(f"\n  PENDING pulses ({len(pulses)}):\n")
    for p in pulses:
        raw_ts = p.get("created_at")
        utc_timestamp = (
            raw_ts[5:16].replace("T", " ") if isinstance(raw_ts, str) and len(raw_ts) >= 16 else "?"
        )
        print(f"  [{utc_timestamp}] {p['pulse_id']}")
        print(f"    [{p['priority']}] {p['instruction'][:80]}")
        if p.get("target_file"):
            print(f"    target: {p['target_file']}")
        r, m = p.get("retry_count", 0), p.get("max_retries", 3)
        print(f"    retry: {r}/{m}" if r > 0 else f"    retry: 0/{m}")
        print()


def cmd_watch_pulse_create(args):
    """새 pulse 생성."""
    pid = log_message(
        source="cli",
        target="operator",
        type="manual",
        content=args.instruction,
        priority=args.priority,
        target_file=args.target_file,
        target_test=args.target_test,
        category=args.category,
    )
    if pid:
        print(f"  Created: {pid}")
    else:
        print("  ERROR: Could not create pulse (duplicate or DB error)")


def cmd_watch_pulse_resolve(args):
    """pulse 완료 처리."""
    status = "IGNORED" if args.ignore else "RESOLVED"
    ok = resolve_pulse(args.pulse_id, status)
    if ok:
        print(f"  {status}: {args.pulse_id}")
    else:
        print(f"  ERROR: Could not resolve {args.pulse_id}")


def cmd_watch_pulse_show(args):
    """pulse 상세 정보."""
    p = get_pulse(args.pulse_id)
    if not p:
        print(f"  Pulse not found: {args.pulse_id}")
        return
    for k, v in p.items():
        if v:
            print(f"  {k}: {v}")


def cmd_watch_log(args):
    """catchdog_events 최근 로그."""
    comp_filter = f"AND component = '{esc_sql(args.component)}'" if args.component else ""
    rows = psql_json(
        f"SELECT component, event_type, from_state, to_state, detail, fail_count, created_at "
        f"FROM catchdog_events "
        f"WHERE 1=1 {comp_filter} "
        f"ORDER BY created_at DESC LIMIT {args.limit}"
    )
    if not rows:
        print("  No events logged")
        return
    print(f"\n  Events ({len(rows)}):\n")
    for r in rows:
        raw_ts = r.get("created_at")
        utc_timestamp = (
            raw_ts[5:19].replace("T", " ") if isinstance(raw_ts, str) and len(raw_ts) >= 19 else "?"
        )
        fmt = f"  [{utc_timestamp}] {r['component']}:{r['event_type']}"
        if r.get("to_state"):
            fmt += f" → {r['to_state']}"
        if r.get("detail"):
            fmt += f" — {r['detail'][:80]}"
        print(fmt)


def cmd_watch_incidents(args):
    """watchdog incident 목록 (감지→조치→결과)."""
    from lib.watchdog import incidents as _inc

    rows = _inc.list_incidents(
        open_only=getattr(args, "open", False),
        since=getattr(args, "since", None),
        limit=getattr(args, "limit", 20),
    )
    if not rows:
        print("  No incidents logged")
        return
    print(f"\n  Incidents ({len(rows)}):\n")
    for r in rows:
        ts = (r.get("detected_at") or "?")[5:19].replace("T", " ")
        flag = "OPEN" if r.get("status") == "open" else "done"
        print(f"  [{ts}] #{r['id']} {flag:4s} {r['component']} — {(r.get('symptom') or '')[:70]}")
        extra = []
        if r.get("action"):
            extra.append(f"action={r['action']}({r.get('action_result') or '?'})")
        if int(r.get("fail_count") or 0) > 1:
            extra.append(f"fails={r['fail_count']}")
        if int(r.get("reopen_count") or 0) > 0:
            extra.append(f"reopens={r['reopen_count']}")
        if extra:
            print("        " + "  ".join(extra))


def cmd_watch_incident_show(args):
    """incident 상세 (컨텍스트/조치 포함)."""
    from lib.watchdog import incidents as _inc

    r = _inc.get_incident(args.incident_id)
    if not r:
        print(f"  Incident not found: {args.incident_id}")
        return
    for k in ("id", "status", "component", "symptom", "action", "action_result",
              "fail_count", "reopen_count", "detected_at", "resolved_at"):
        if r.get(k) not in (None, ""):
            print(f"  {k}: {r[k]}")
    if r.get("context"):
        print("\n  --- context (masked) ---")
        print("  " + (r["context"] or "").replace("\n", "\n  "))
