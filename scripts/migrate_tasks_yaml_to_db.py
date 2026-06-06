#!/usr/bin/env python3
# Status: production
# Path: manual — one-shot migration
"""One-shot: migrate docs/tasks.yaml → DB tasks table."""
import yaml, sys, json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.db import psql_ok, esc_sql

TASKS_PATH = Path("/opt/projects/server/docs/tasks.yaml")

def main():
    raw = TASKS_PATH.read_text()
    if raw.startswith("#"):
        _, _, raw = raw.partition("\n")
    data = yaml.safe_load(raw)
    if not data or "tasks" not in data:
        print("ERROR: tasks.yaml empty or invalid")
        sys.exit(1)

    count = 0
    for t in data["tasks"]:
        title = esc_sql(t["title"])
        status = t.get("status", "pending")
        priority = t.get("priority", "")
        desc = esc_sql(t.get("description", ""))
        notes_list = t.get("notes", [])
        notes_json = json.dumps(notes_list) if notes_list else "[]"

        sql = f"""INSERT INTO tasks (title, status, priority, description, notes)
        VALUES ('{title}', '{status}', '{priority}', '{desc}', '{notes_json}'::jsonb)
        ON CONFLICT (title) DO UPDATE SET
            status = EXCLUDED.status,
            priority = EXCLUDED.priority,
            description = EXCLUDED.description,
            notes = EXCLUDED.notes,
            updated_at = NOW()
        RETURNING id"""
        result = psql_ok(sql)
        if result:
            print(f"  OK: {title[:60]}")
            count += 1
        else:
            print(f"  FAIL: {title[:60]}")

    psql_ok("UPDATE tasks SET completed_at = NOW() WHERE status = 'completed' AND completed_at IS NULL")
    print(f"\nMigrated {count}/{len(data['tasks'])} tasks")

if __name__ == "__main__":
    main()
