#!/usr/bin/env python3
# Status: production
# Path: manual — one-shot migration
"""One-shot: migrate docs/specs/references.yaml → static_references table."""
import yaml, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.db import psql_ok, esc_sql

REFS_PATH = Path("/opt/projects/server/docs/specs/references.yaml")

def main():
    raw = REFS_PATH.read_text()
    if raw.startswith("#"):
        _, _, raw = raw.partition("\n")
    data = yaml.safe_load(raw)
    if not data:
        print("ERROR: references.yaml empty or invalid")
        sys.exit(1)

    count = 0
    for category, items in data.items():
        if category == "metadata":
            continue
        if not isinstance(items, list):
            continue
        for ref in items:
            name = esc_sql(ref.get("name", ""))
            url = esc_sql(ref.get("url", ""))
            desc = esc_sql(ref.get("description", ""))
            sql = f"""INSERT INTO static_references (category, name, url, description)
            VALUES ('{esc_sql(category)}', '{name}', '{url}', '{desc}')
            ON CONFLICT (category, name) DO UPDATE SET
                url = EXCLUDED.url, description = EXCLUDED.description"""
            ok = psql_ok(sql)
            if ok:
                count += 1
            else:
                print(f"  FAIL: [{category}] {name[:60]}")

    print(f"\nMigrated {count} references")

if __name__ == "__main__":
    main()
