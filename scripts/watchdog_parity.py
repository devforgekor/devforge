#!/usr/bin/env python3
# Status: experimental
# Path: none — 24h watchdog v2 shadow parity harness (Phase 2.5 gate). Run manually after the window.
"""watchdog_parity.py — v2 dry-run detections vs legacy watchdog incidents.

Compares, over the shadow window, the components that watchdog v2 flagged in
dry-run (parsed from its journal) against the incidents the legacy watchdog
actually recorded in Postgres (`watchdog_incidents`).

Because v2 runs dry-run it never writes incidents, so the journal is the only
v2 signal. Parity is **component-set based** (v2 logs the same component every
cycle while unhealthy; legacy records one incident per episode, so raw counts
are not directly comparable).

Exit 0 iff every v2_only component is an expected alert-only family (legacy
records alerts, not incidents, for memory/llm/disk/heartbeat) and there is no
legacy_only component; any other v2_only is a detection gap and exits 1.

Usage:
  python3 scripts/watchdog_parity.py
  python3 scripts/watchdog_parity.py --since 2026-09-24T23:58:36Z
  python3 scripts/watchdog_parity.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

sys.path.insert(0, "/opt/projects/server/scripts")

from lib.db import psql_json  # noqa: E402

DEFAULT_SERVICE = "devforge-watchdog-v2.service"
DRY_RUN_RE = re.compile(r"\[dry-run\]\s+(\S+)\s+failed:\s*(.*?)\s*\(would\s+([^)]+)\)")


def parse_dry_run_line(line: str) -> Optional[dict[str, str]]:
    """Extract {component, detail, would} from one v2 dry-run log line."""
    m = DRY_RUN_RE.search(line)
    if not m:
        return None
    return {"component": m.group(1), "detail": m.group(2).strip(), "would": m.group(3).strip()}


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _journal_lines(service: str, since: datetime, until: datetime) -> list[str]:
    # [WHY] @epoch: journalctl rejects an explicit "+00:00" ISO offset.
    cmd = [
        "journalctl", "--user", "-u", service,
        "--since", f"@{int(since.timestamp())}", "--until", f"@{int(until.timestamp())}",
        "--no-pager",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"journalctl failed: {e}", file=sys.stderr)
        return []
    if r.returncode != 0:
        print(f"journalctl rc={r.returncode}: {(r.stderr or '').strip()[:200]}", file=sys.stderr)
    return (r.stdout or "").splitlines()


def _legacy_rows(since: datetime, until: datetime) -> list[dict[str, Any]]:
    query = (
        "SELECT component, status, count(*) AS count, "
        "min(detected_at)::text AS first, max(detected_at)::text AS last, "
        "coalesce(sum(reopen_count), 0) AS reopens "
        "FROM watchdog_incidents "
        f"WHERE (detected_at >= '{since.isoformat()}' AND detected_at <= '{until.isoformat()}') "
        "OR status = 'open' "
        "GROUP BY component, status ORDER BY component"
    )
    return psql_json(query)


def _legacy_ever() -> set[str]:
    """All components legacy has ever recorded (window-independent)."""
    rows = psql_json("SELECT DISTINCT component FROM watchdog_incidents")
    return {r["component"] for r in rows}


# [WHY] legacy orchestrator writes incidents only for these prefixes; memory,
# llm, disk, heartbeat and dataimpulse failures go to send_alert() only, so they
# can never appear in watchdog_incidents — v2_only for them is not a detection
# bug but a record-policy difference.
LEGACY_INCIDENT_PREFIXES = ("svc:", "timer:", "oneshot:", "syssvc:")


def classify_v2_only(component: str, legacy_ever: set[str]) -> str:
    if not component.startswith(LEGACY_INCIDENT_PREFIXES):
        return "alert_only"
    if component in legacy_ever:
        return "parity_gap"
    return "never_recorded"


def build_report(
    v2_lines: list[str],
    legacy_rows: list[dict[str, Any]],
    legacy_ever: Optional[set[str]] = None,
) -> dict[str, Any]:
    v2_counts: dict[str, int] = {}
    v2_samples: dict[str, str] = {}
    for line in v2_lines:
        d = parse_dry_run_line(line)
        if not d:
            continue
        v2_counts[d["component"]] = v2_counts.get(d["component"], 0) + 1
        v2_samples.setdefault(d["component"], d["detail"])

    legacy_counts = {r["component"]: int(r["count"]) for r in legacy_rows}
    legacy_reopens = {r["component"]: int(r["reopens"]) for r in legacy_rows}

    v2_set, legacy_set = set(v2_counts), set(legacy_counts)
    matched = sorted(v2_set & legacy_set)
    v2_only = sorted(v2_set - legacy_set)
    legacy_only = sorted(legacy_set - v2_set)
    ever = legacy_ever if legacy_ever is not None else set()
    v2_only_reasons = {c: classify_v2_only(c, ever) for c in v2_only}
    # alert_only families are an expected record-policy difference, not a
    # detection gap, so they do not fail the gate.
    gate_failures = [c for c in v2_only if v2_only_reasons[c] != "alert_only"]

    components = [
        {
            "component": comp,
            "v2_count": v2_counts.get(comp, 0),
            "legacy_count": legacy_counts.get(comp, 0),
            "legacy_reopens": legacy_reopens.get(comp, 0),
            "verdict": "matched"
            if comp in v2_set and comp in legacy_set
            else ("v2_only" if comp in v2_set else "legacy_only"),
            "reason": v2_only_reasons.get(comp, ""),
        }
        for comp in sorted(v2_set | legacy_set)
    ]
    return {
        "v2_components": len(v2_set),
        "legacy_components": len(legacy_set),
        "matched": matched,
        "v2_only": v2_only,
        "v2_only_reasons": v2_only_reasons,
        "gate_failures": gate_failures,
        "legacy_only": legacy_only,
        "components": components,
        "v2_samples": v2_samples,
    }


def _print_report(rep: dict[str, Any], since: datetime, until: datetime, service: str) -> None:
    print("# watchdog v2 shadow parity")
    print(f"# window : {since.isoformat()} ~ {until.isoformat()}")
    print(f"# v2 svc : {service} (dry-run)")
    print(
        f"# v2={rep['v2_components']} legacy={rep['legacy_components']} "
        f"matched={len(rep['matched'])} v2_only={len(rep['v2_only'])} legacy_only={len(rep['legacy_only'])}"
    )
    print(
        f"# gate   : detection_gaps={len(rep['gate_failures'])} "
        f"legacy_only={len(rep['legacy_only'])} (alert_only excluded by record policy)"
    )
    print()
    print(f"{'component':<44}{'v2':>5}{'legacy':>8}{'reopen':>8}  verdict      detail")
    for c in rep["components"]:
        detail = rep["v2_samples"].get(c["component"], "")
        print(
            f"{c['component']:<44}{c['v2_count']:>5}{c['legacy_count']:>8}{c['legacy_reopens']:>8}"
            f"  {c['verdict']:<12} {detail[:40]}"
        )
    if rep["v2_only"]:
        print("\n[!] v2_only (v2 flagged, legacy never recorded):")
        print("    alert_only = legacy records only alerts for this family (policy)")
        print("    parity_gap = legacy recorded it before (window/state semantics)")
        print("    never_recorded = legacy checks it but has never recorded")
        for comp in rep["v2_only"]:
            reason = rep["v2_only_reasons"].get(comp, "")
            print(f"    {comp} [{reason}]: {rep['v2_samples'].get(comp, '')}")
    if rep["legacy_only"]:
        print("\n[!] legacy_only (legacy recorded, v2 never flagged — possible miss):")
        for comp in rep["legacy_only"]:
            print(f"    {comp}")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="watchdog v2 dry-run vs legacy incident parity")
    ap.add_argument("--since", default=None, help="ISO start (default: 24h ago, UTC)")
    ap.add_argument("--until", default=None, help="ISO end (default: now, UTC)")
    ap.add_argument("--service", default=DEFAULT_SERVICE, help="v2 systemd unit name")
    ap.add_argument("--json", action="store_true", help="emit JSON report")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    since = parse_iso(args.since) if args.since else now - timedelta(hours=24)
    until = parse_iso(args.until) if args.until else now

    rep = build_report(
        _journal_lines(args.service, since, until), _legacy_rows(since, until), _legacy_ever()
    )
    if args.json:
        print(json.dumps({"since": since.isoformat(), "until": until.isoformat(), **rep}, indent=2, default=str))
    else:
        _print_report(rep, since, until, args.service)

    return 0 if not rep["gate_failures"] and not rep["legacy_only"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
