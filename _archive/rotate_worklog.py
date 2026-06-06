#!/usr/bin/env python3
"""Worklog rotation: keeps worklog.json at 3 entries, archives overflow to DB via psql."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

WORKLOG = Path(__file__).resolve().parent.parent / "docs" / "worklog.json"

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres", "-d", "devforge_app",
        "--no-align", "--tuples-only", "--quiet"]


def _escape_sql(value: str) -> str:
    """Escape a string for safe embedding in a PostgreSQL SQL literal."""
    return value.replace("'", "''").replace("\\", "\\\\")


def _insert_one(entry: dict) -> bool:
    """Insert single entry into worklog_entries. Returns True if inserted."""
    date = _escape_sql(entry["date"])
    title = _escape_sql(entry["title"])
    summary = _escape_sql(entry["summary"])
    details_json = json.dumps(entry.get("details", []))
    files_json = json.dumps(entry.get("files", []))
    tags_array = "{" + ",".join(entry.get("tags", [])) + "}"

    sql = f"""
    INSERT INTO worklog_entries (date, title, summary, details, files, tags)
    VALUES ('{date}', '{title}', '{summary}', '{details_json}'::jsonb, '{files_json}'::jsonb, '{tags_array}')
    ON CONFLICT (date, title) DO NOTHING
    RETURNING id
    """
    proc = subprocess.run(
        PSQL + ["-c", sql],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        print(f"  [worklog] DB error: {proc.stderr.strip()}", file=sys.stderr)
        return False
    return bool(proc.stdout.strip())


def rotate():
    if not WORKLOG.exists():
        print("  [worklog] worklog.json not found, skip")
        return

    with open(WORKLOG) as f:
        data = json.load(f)

    entries = data.get("entries", [])
    if len(entries) <= 3:
        print(f"  [worklog] {len(entries)} entries, no rotation needed")
        return

    archived = 0
    while len(entries) > 3:
        oldest = entries[0]
        ok = _insert_one(oldest)
        if not ok:
            print(f"  [worklog] {oldest['date']} {oldest['title'][:60]} → DB (skipped, stop rotation)")
            break
        entries.pop(0)
        archived += 1
        print(f"  [worklog] {oldest['date']} {oldest['title'][:60]} → DB (archived)")

    data["entries"] = entries
    data["updated"] = datetime.now(timezone.utc).isoformat()

    with open(WORKLOG, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"  [worklog] saved: {len(entries)} entries, {archived} archived to DB")


if __name__ == "__main__":
    rotate()
