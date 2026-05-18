#!/usr/bin/env python3
"""link_turns.py — nightly: match turns to worklog entries + deep review.

Phase 1 — matching: each worklog entry claims turns with matching agent created
between the previous worklog's created_at (or KST midnight) and this worklog's created_at.
Phase 2 — review: detect orphan turns, empty worklogs, and mismatches.

Runs at 03:00 KST via nightly_batch.sh.
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.db import psql

LOG_FILE = Path("/opt/projects/server/link_turns.log")
REVIEW_FILE = Path("/opt/projects/server/docs/link_review.yaml")

def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _match(kst_start, kst_end, today_kst):
    """Phase 1: match today's worklog entries to turns by agent + time window."""
    rows = psql(
        f"SELECT id, agent, created_at FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz "
        f"ORDER BY created_at"
    )
    if not rows:
        _log(f"No worklog entries for {today_kst}")
        return 0

    entries = []
    for line in rows.split("\n"):
        parts = line.split("|")
        if len(parts) >= 3:
            entries.append({"id": parts[0], "agent": parts[1], "ts": parts[2]})

    if not entries:
        _log(f"No worklog entries for {today_kst}")
        return 0

    total_linked = 0

    for i, entry in enumerate(entries):
        entry_id = entry["id"]
        source = entry["agent"]
        ts = entry["ts"]

        if i > 0:
            window_sql = (f"AND t.created_at > '{entries[i-1]['ts']}' "
                          f"AND t.created_at <= '{ts}'")
        else:
            window_sql = (
                f"AND t.created_at > GREATEST("
                f"  '{ts}'::timestamptz - INTERVAL '12 hours',"
                f"  '{kst_start}'::timestamptz"
                f") AND t.created_at <= '{ts}'"
            )

        sql = (
            f"SELECT t.id FROM turns t "
            f"WHERE t.agent = '{source}' "
            f"{window_sql} "
            f"  AND t.id NOT IN ("
            f"    SELECT unnest(COALESCE(turn_ids, '{{}}'::uuid[])) "
            f"    FROM worklog_entries WHERE turn_ids IS NOT NULL"
            f"  ) "
            f"ORDER BY t.created_at"
        )
        turn_rows = psql(sql)

        if turn_rows:
            turn_ids = [t for t in turn_rows.split("\n") if t]
            if turn_ids:
                ids_array = "{" + ",".join(turn_ids) + "}"
                psql(
                    f"UPDATE worklog_entries SET turn_ids = turn_ids || "
                    f"'{ids_array}'::uuid[] WHERE id = {entry_id}"
                )
                _log(f"worklog {entry_id} ({source}): linked {len(turn_ids)} turns")
                total_linked += len(turn_ids)

    _log(f"Matched: {total_linked} turns across {len(entries)} worklog entries")
    return total_linked


def _review(kst_start, kst_end, today_kst):
    """Phase 2: deep review — find orphans, empties, anomalies."""
    findings = []

    # Orphan turns: yesterday's turns not linked to any worklog
    orphans = psql(
        f"SELECT t.agent, COUNT(*) FROM turns t "
        f"WHERE t.created_at >= '{kst_start}'::timestamptz "
        f"  AND t.created_at <  '{kst_end}'::timestamptz "
        f"  AND t.id NOT IN ("
        f"    SELECT unnest(turn_ids) FROM worklog_entries "
        f"    WHERE turn_ids IS NOT NULL AND array_length(turn_ids, 1) > 0"
        f"  ) "
        f"GROUP BY t.agent ORDER BY COUNT(*) DESC"
    )
    if orphans:
        for line in orphans.split("\n"):
            if "|" in line:
                agent, cnt = line.split("|", 1)
                findings.append(f"orphan_turns.{agent}: {cnt}")
    else:
        findings.append("orphan_turns: 0")

    # Empty worklog: today's worklog entries with zero linked turns
    empties = psql(
        f"SELECT id, title FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz "
        f"  AND (turn_ids IS NULL OR array_length(turn_ids, 1) IS NULL "
        f"       OR array_length(turn_ids, 1) = 0)"
    )
    if empties:
        for line in empties.split("\n"):
            if "|" in line:
                wid, title = line.split("|", 1)
                findings.append(f"empty_worklog: #{wid} {title}")
    else:
        findings.append("empty_worklog: 0")

    # Stats: total turns vs linked turns for the review window
    total = psql(
        f"SELECT COUNT(*) FROM turns "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz"
    )
    linked = psql(
        f"SELECT COUNT(*) FROM turns t "
        f"WHERE t.created_at >= '{kst_start}'::timestamptz "
        f"  AND t.created_at <  '{kst_end}'::timestamptz "
        f"  AND t.id IN ("
        f"    SELECT unnest(turn_ids) FROM worklog_entries "
        f"    WHERE turn_ids IS NOT NULL AND array_length(turn_ids, 1) > 0"
        f"  )"
    )
    findings.append(f"turn_coverage: {linked}/{total}")

    # Cross-source mismatch: worklog with agent=X but no turns from agent X today
    agents = psql(
        f"SELECT DISTINCT agent FROM turns "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz"
    )
    worklog_agents = psql(
        f"SELECT DISTINCT agent FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz"
    )
    if agents and worklog_agents:
        turn_agents = set(agents.split("\n"))
        wl_agents = set(worklog_agents.split("\n"))
        only_turns = turn_agents - wl_agents
        only_wl = wl_agents - turn_agents
        if only_turns:
            findings.append(f"turns_without_worklog_agent: {', '.join(only_turns)}")
        if only_wl:
            findings.append(f"worklog_without_turns_agent: {', '.join(only_wl)}")

    _log(f"Review: {'; '.join(findings)}")
    return findings


def main():
    kst = timezone(timedelta(hours=9))
    now_kst = datetime.now(kst)
    # At 03:00 KST, review the completed previous day
    review_date = (now_kst - timedelta(days=1)).strftime("%Y-%m-%d")
    kst_start = f"{review_date} 00:00:00+09"
    kst_end = f"{now_kst:%Y-%m-%d} 00:00:00+09"

    _log(f"=== link_turns {review_date} ===")

    # Phase 1: matching
    linked = _match(kst_start, kst_end, review_date)

    # Phase 2: deep review
    findings = _review(kst_start, kst_end, review_date)

    # Write review file (consumed by session_start + 9am Slack)
    lines = ["# link_turns review", f"date: {review_date}", f"linked: {linked}"]
    lines.extend(f"finding: {f}" for f in findings)
    REVIEW_FILE.write_text("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
