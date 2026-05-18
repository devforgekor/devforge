#!/usr/bin/env python3
"""monitor_health.py — idempotent system health snapshot.

Run anytime. No side effects. Reads only.
Output: YAML to stdout, suitable for diff comparison between runs.
"""
import subprocess
import sys
from datetime import datetime, timezone

from lib.db import psql


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(s):
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return -1


def check_orphans():
    """Turns not linked to any worklog."""
    r = psql(
        "SELECT t.agent, COUNT(*) FROM turns t "
        "WHERE t.id NOT IN ("
        "  SELECT unnest(turn_ids) FROM worklog_entries "
        "  WHERE turn_ids IS NOT NULL AND array_length(turn_ids, 1) > 0"
        ") GROUP BY t.agent ORDER BY COUNT(*) DESC"
    )
    if not r:
        return {"total": 0, "by_agent": {}}
    by_agent = {}
    total = 0
    for line in r.split("\n"):
        if "|" in line:
            agent, cnt = line.split("|", 1)
            c = _int(cnt)
            by_agent[agent] = c
            total += c
    return {"total": total, "by_agent": by_agent}


def check_coverage():
    """Turn coverage ratio."""
    total = _int(psql("SELECT COUNT(*) FROM turns"))
    linked = _int(psql(
        "SELECT COUNT(*) FROM turns t "
        "WHERE t.id IN ("
        "  SELECT unnest(turn_ids) FROM worklog_entries "
        "  WHERE turn_ids IS NOT NULL AND array_length(turn_ids, 1) > 0"
        ")"
    ))
    pct = round(100.0 * linked / total, 2) if total > 0 else 0
    return {"total_turns": total, "linked": linked, "coverage_pct": pct}


def check_commits():
    """Commit recording rate."""
    recorded = _int(psql(
        "SELECT COUNT(*) FROM worklog_entries WHERE git_commit_hash IS NOT NULL"
    ))
    # Count git commits in last 7 days
    r = subprocess.run(
        ["git", "-C", "/opt/projects/server", "log", "--since=7 days ago", "--oneline"],
        capture_output=True, text=True
    )
    git_total = len([l for l in r.stdout.split("\n") if l.strip()])
    return {
        "git_commits_7d": git_total,
        "worklog_recorded": recorded,
        "recording_rate_pct": round(100.0 * recorded / git_total, 1) if git_total > 0 else 0
    }


def check_worklogs():
    """Worklog entry classification."""
    empty = _int(psql(
        "SELECT COUNT(*) FROM worklog_entries "
        "WHERE (turn_ids IS NULL OR array_length(turn_ids, 1) IS NULL "
        "       OR array_length(turn_ids, 1) = 0)"
        "  AND git_commit_hash IS NULL"
    ))
    commit_only = _int(psql(
        "SELECT COUNT(*) FROM worklog_entries "
        "WHERE (turn_ids IS NULL OR array_length(turn_ids, 1) IS NULL "
        "       OR array_length(turn_ids, 1) = 0)"
        "  AND git_commit_hash IS NOT NULL"
    ))
    agentless = _int(psql(
        "SELECT COUNT(*) FROM worklog_entries "
        "WHERE agent IS NULL OR agent = ''"
    ))
    return {
        "true_empty": empty,
        "commit_only": commit_only,
        "agentless": agentless,
        "total": _int(psql("SELECT COUNT(*) FROM worklog_entries"))
    }


def check_review_facts():
    """Review pipeline accuracy."""
    r = psql(
        "SELECT extract_model, verdict, COUNT(*) FROM review_facts "
        "GROUP BY extract_model, verdict ORDER BY extract_model, verdict"
    )
    models = {}
    if r:
        for line in r.split("\n"):
            if "|" in line:
                parts = line.split("|")
                if len(parts) >= 3:
                    model, verdict, cnt = parts[0], parts[1], _int(parts[2])
                    if model not in models:
                        models[model] = {}
                    models[model][verdict] = cnt
    return models


def check_timers():
    """Critical timer status using systemctl show for reliable parsing."""
    timer_units = [
        "review-worker.timer", "devforge-collect.timer",
        "devforge-nightly.timer", "devforge-qwen-worker.timer"
    ]
    timers = {}
    for unit in timer_units:
        r = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "--property=ActiveState,NextElapseUSecRealtime,LastTriggerUSec"],
            capture_output=True, text=True
        )
        info = {}
        for line in r.stdout.split("\n"):
            if "=" in line:
                k, v = line.split("=", 1)
                info[k] = v
        status = info.get("ActiveState", "unknown")
        # systemctl show returns human-readable timestamps for timer properties
        next_ts = info.get("NextElapseUSecRealtime", "")
        last_ts = info.get("LastTriggerUSec", "")
        timers[unit] = {"status": status, "next": next_ts, "last": last_ts}
    return timers


def main():
    print(f"# health snapshot: {_now()}")
    print()

    print("orphans:")
    o = check_orphans()
    print(f"  total: {o['total']}")
    if o['by_agent']:
        print(f"  by_agent:")
        for agent, cnt in o['by_agent'].items():
            print(f"    {agent}: {cnt}")

    print()
    print("coverage:")
    c = check_coverage()
    print(f"  total_turns: {c['total_turns']}")
    print(f"  linked: {c['linked']}")
    print(f"  coverage_pct: {c['coverage_pct']}")

    print()
    print("commits:")
    cm = check_commits()
    print(f"  git_commits_7d: {cm['git_commits_7d']}")
    print(f"  worklog_recorded: {cm['worklog_recorded']}")
    print(f"  recording_rate_pct: {cm['recording_rate_pct']}")

    print()
    print("worklogs:")
    w = check_worklogs()
    print(f"  total: {w['total']}")
    print(f"  true_empty: {w['true_empty']}")
    print(f"  commit_only: {w['commit_only']}")
    print(f"  agentless: {w['agentless']}")

    print()
    print("review_facts:")
    rf = check_review_facts()
    for model, verdicts in rf.items():
        valid = verdicts.get("valid", 0)
        hallu = verdicts.get("hallucinated", 0)
        total_m = sum(verdicts.values())
        acc = round(100.0 * valid / (valid + hallu), 1) if (valid + hallu) > 0 else 0
        print(f"  {model}:")
        for v, cnt in verdicts.items():
            print(f"    {v}: {cnt}")
        print(f"    accuracy_pct: {acc}")

    print()
    print("timers:")
    for name, info in check_timers().items():
        print(f"  {name}:")
        print(f"    next: {info['next']}")
        print(f"    last: {info['last']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
