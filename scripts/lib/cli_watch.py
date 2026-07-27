# Status: production
# Path: imported by — cli.py (watch subcommand)
"""CLI watch commands — server status, alerts, pulse queue, event log."""

from lib.db import esc_sql, psql_json
from lib.watchdog.messenger import get_pulse, list_pulses, log_message, resolve_pulse


def cmd_watch_status(args):
    """Server survival + pulse queue + events."""
    rows = psql_json(
        "SELECT pulse_id, priority, instruction, status, "
        "created_at::text, retry_count, max_retries "
        "FROM watchdog_pulses "
        "WHERE status IN ('PENDING', 'IN_PROGRESS', 'HUMAN_REQUIRED') "
        "ORDER BY "
        "  CASE priority "
        "    WHEN 'P0_HOT_FIX' THEN 1 "
        "    WHEN 'P1_CONTEXT' THEN 2 "
        "    WHEN 'HUMAN_REQUIRED' THEN 3 "
        "    ELSE 4 END, "
        "  created_at DESC"
    )
    events = psql_json(
        "SELECT event_type, detail, created_at::text "
        "FROM watchdog_events "
        "WHERE created_at > now() - interval '1 hour' "
        "ORDER BY created_at DESC"
    )
    print(f"{'Pulse ID':<45} {'Priority':<16} {'Status':<16} {'Instruction':<60} {'Retry'}")
    print("-" * 140)
    for r in rows or []:
        print(
            f"{(r['pulse_id'] or '')[:42]:<45} "
            f"{r['priority']:<16} "
            f"{r['status']:<16} "
            f"{(r['instruction'] or '')[:58]:<60} "
            f"{r['retry_count']}/{r['max_retries']}"
        )
    print(f"\nTotal pulses: {len(rows or [])}")
    if events:
        print(f"\nLast {len(events)} events (1h):")
        for e in events:
            print(f"  {e['created_at']} [{e['event_type']}] {e['detail'][:80]}")
    else:
        print("\nNo events in last hour")


def cmd_watch_alerts(args):
    """List PENDING/HUMAN_REQUIRED pulses needing attention."""
    for status in ("PENDING", "HUMAN_REQUIRED"):
        rows = list_pulses(status=status)
        if rows:
            print(f"── {status} ({len(rows)}) ──")
            for r in rows:
                print(f"  {r['pulse_id']}: [{r['priority']}] {r['instruction'][:80]}")
                if r.get("retry_count", 0) > 0:
                    print(f"    retry={r['retry_count']}/{r['max_retries']}")
        else:
            print(f"── {status} (0) ──")


def cmd_watch_pulses_list(args):
    """List PENDING pulses."""
    rows = list_pulses(limit=args.limit)
    if not rows:
        print("(no pending pulses)")
        return
    print(
        f"{'ID':<42} {'Priority':<14} {'Category':<14} {'Instruction':<60} {'Retry':<8} {'Created'}"
    )
    print("-" * 150)
    for r in rows:
        print(
            f"{(r['pulse_id'] or '')[:42]:<42} "
            f"{r['priority']:<14} "
            f"{(r.get('category') or ''):<14} "
            f"{(r['instruction'] or '')[:58]:<60} "
            f"{r['retry_count']}/{r['max_retries']:<5} "
            f"{(r.get('created_at') or '')[:19]}"
        )


def cmd_watch_pulse_create(args):
    """Create a new pulse."""
    pulse_id = log_message(
        source="cli",
        target="operator",
        type="alert",
        content=args.instruction,
        category=args.category,
        target_file=args.target_file,
        target_test=args.target_test,
        priority=args.priority,
    )
    if pulse_id:
        print(f"Created pulse: {pulse_id}")
    else:
        print("ERROR: pulse already exists (duplicate)")


def cmd_watch_pulse_resolve(args):
    """Resolve or ignore a pulse."""
    status = "IGNORED" if args.ignore else "RESOLVED"
    if resolve_pulse(args.pulse_id, status=status):
        print(f"Pulse {args.pulse_id}: {status}")
    else:
        print(f"ERROR: could not resolve {args.pulse_id}")


def cmd_watch_pulse_show(args):
    """Show pulse details."""
    r = get_pulse(args.pulse_id)
    if not r:
        print(f"ERROR: pulse {args.pulse_id} not found")
        return
    for k, v in r.items():
        print(f"  {k}: {v}")


def cmd_watch_log(args):
    """Recent watchdog events."""
    components = ""
    if args.component:
        components = f"AND component = '{esc_sql(args.component)}'"
    rows = psql_json(
        f"SELECT event_type, component, detail, created_at::text "
        f"FROM watchdog_events "
        f"WHERE created_at > now() - interval '24 hours' {components} "
        f"ORDER BY created_at DESC LIMIT {args.limit}"
    )
    if not rows:
        print("(no recent events)")
        return
    print(f"{'Time':<20} {'Type':<24} {'Component':<20} {'Detail'}")
    print("-" * 130)
    for r in rows:
        print(
            f"{(r['created_at'] or '')[:19]:<20} "
            f"{(r['event_type'] or '')[:22]:<24} "
            f"{(r.get('component') or '')[:18]:<20} "
            f"{(r['detail'] or '')[:70]}"
        )
