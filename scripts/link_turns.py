#!/usr/bin/env python3
# Status: production
# Path: nightly_batch.sh
"""link_turns.py — nightly: match turns to worklog entries + deep review.

Phase 1 — matching: each worklog entry claims turns with matching agent
via per-agent independent time windows (last entry covers to midnight KST).
Phase 2 — review: detect orphan turns, empty worklogs, and mismatches.

Runs at 03:00 KST via nightly_batch.sh.
"""
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib.db import psql, psql_json

LOG_FILE = Path("/opt/projects/server/link_turns.log")
REVIEW_FILE = Path("/opt/projects/server/data/link_review.yaml")

def _log(msg):
    utc_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{utc_ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def match_turns_to_worklog_entries(kst_start, kst_end, today_kst):
    """Phase 1: per-agent independent time windows.

    Each worklog claims unlinked turns of its agent between the previous
    same-agent worklog's created_at (or kst_start for the first) and its own
    created_at. The last worklog of each agent covers to kst_end (midnight).
    """
    rows = psql_json(
        f"SELECT id, agent, created_at FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz "
        f"ORDER BY created_at"
    )
    if not rows:
        _log(f"No worklog entries for {today_kst}")
        return 0

    entries = [{"id": row["id"], "agent": row.get("agent", ""), "ts": row["created_at"]}
               for row in rows]

    if not entries:
        _log(f"No worklog entries for {today_kst}")
        return 0

    agent_last_idx = {}
    for i, e in enumerate(entries):
        agent = e["agent"]
        if agent:
            agent_last_idx[agent] = i

    total_linked = 0

    for i, entry in enumerate(entries):
        entry_id = entry["id"]
        agent = entry["agent"]

        if not agent:
            _log(f"worklog {entry_id}: skipped (no agent)")
            continue

        # Find previous same-agent worklog
        prev_entry_ts = None
        for j in range(i - 1, -1, -1):
            if entries[j]["agent"] == agent:
                prev_entry_ts = entries[j]["ts"]
                break

        entry_ts = entry["ts"]
        is_last = (agent_last_idx.get(agent) == i)

        if prev_entry_ts:
            window_sql = (
                f"AND t.created_at > '{prev_entry_ts}' "
                f"AND t.created_at <= '{entry_ts}'"
            )
        else:
            window_sql = (
                f"AND t.created_at > GREATEST("
                f"  '{entry_ts}'::timestamptz - INTERVAL '12 hours',"
                f"  '{kst_start}'::timestamptz"
                f") AND t.created_at <= '{ts}'"
            )

        # Last worklog for this agent: extend upper bound to kst_end
        if is_last:
            window_sql = window_sql.replace(
                f"AND t.created_at <= '{ts}'",
                f"AND t.created_at <  '{kst_end}'::timestamptz"
            )

        sql = (
            f"SELECT t.id FROM turns t "
            f"WHERE t.agent = '{agent}' "
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
                _log(f"worklog {entry_id} ({agent}): linked {len(turn_ids)} turns")
                total_linked += len(turn_ids)

    _log(f"Matched: {total_linked} turns across {len(entries)} worklog entries")
    return total_linked


def audit_link_health(kst_start, kst_end, today_kst):
    """Phase 2: deep review — find orphans, empties, anomalies."""
    findings = []

    # Orphan turns: turns not linked to any worklog
    orphans = psql_json(
        f"SELECT t.agent AS agent, COUNT(*) AS cnt FROM turns t "
        f"WHERE t.created_at >= '{kst_start}'::timestamptz "
        f"  AND t.created_at <  '{kst_end}'::timestamptz "
        f"  AND t.id NOT IN ("
        f"    SELECT unnest(turn_ids) FROM worklog_entries "
        f"    WHERE turn_ids IS NOT NULL AND array_length(turn_ids, 1) > 0"
        f"  ) "
        f"GROUP BY t.agent ORDER BY COUNT(*) DESC"
    )
    if orphans:
        for row in orphans:
            findings.append(f"orphan_turns.{row['agent']}: {row['cnt']}")
    else:
        findings.append("orphan_turns: 0")

    # Empty worklog: no turns AND no git_commit_hash
    empties = psql_json(
        f"SELECT id, title FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz "
        f"  AND (turn_ids IS NULL OR array_length(turn_ids, 1) IS NULL "
        f"       OR array_length(turn_ids, 1) = 0)"
        f"  AND git_commit_hash IS NULL"
    )
    if empties:
        for row in empties:
            findings.append(f"empty_worklog: #{row['id']} {row['title']}")
    else:
        findings.append("empty_worklog: 0")

    no_agent = psql_json(
        f"SELECT id, title FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz "
        f"  AND (agent IS NULL OR agent = '')"
    )
    if no_agent:
        for row in no_agent:
            findings.append(f"agentless_worklog: #{row['id']} {row['title']}")

    # Commit-only worklog: has git_commit_hash but no turns (correct state)
    commit_only = psql(
        f"SELECT id FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz "
        f"  AND git_commit_hash IS NOT NULL"
        f"  AND (turn_ids IS NULL OR array_length(turn_ids, 1) IS NULL "
        f"       OR array_length(turn_ids, 1) = 0)"
    )
    if commit_only:
        ids = [line for line in commit_only.split("\n") if line]
        findings.append(f"commit_only_worklog: {', '.join('#' + i for i in ids)}")

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

    # Cross-source mismatch: worklog with agent=X but no turns from agent X
    agents = psql(
        f"SELECT DISTINCT agent FROM turns "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz"
    )
    worklog_agents = psql(
        f"SELECT DISTINCT agent FROM worklog_entries "
        f"WHERE created_at >= '{kst_start}'::timestamptz "
        f"  AND created_at <  '{kst_end}'::timestamptz"
        f"  AND agent IS NOT NULL AND agent != ''"
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
    # Derive end from start+24h to guarantee a full window (prevents
    # kst_start==kst_end when processing today's date manually)
    end_dt = datetime.strptime(review_date, "%Y-%m-%d") + timedelta(days=1)
    kst_end = f"{end_dt:%Y-%m-%d} 00:00:00+09"

    _log(f"=== link_turns {review_date} ===")

    # Phase 1: matching
    linked = match_turns_to_worklog_entries(kst_start, kst_end, review_date)

    # Phase 2: deep review
    findings = audit_link_health(kst_start, kst_end, review_date)

    # Write review file (consumed by session_start + 9am Slack)
    lines = ["# link_turns review", f"date: {review_date}", f"linked: {linked}"]
    lines.extend(f"finding: {f}" for f in findings)
    REVIEW_FILE.write_text("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
