#!/usr/bin/env python3
# Status: experimental
# Path: none — one-off migration
"""One-time migration: parse str(dict) decision_text into structured columns.

Adds decision_id, detail, status, archived_at columns (already applied via ALTER TABLE).
Parses existing {''id'': ''...'', ''detail'': ''...''} text blobs into separate fields.
"""

import ast
import sys
from lib.db import psql_json, psql


def parse_decision_text(text: str):
    """Try to parse a Python dict repr like {'id': 'nd01-01', 'detail': '...'}."""
    if not text or not text.startswith("{"):
        return None
    try:
        d = ast.literal_eval(text)
        if isinstance(d, dict) and "id" in d:
            return d
    except (ValueError, SyntaxError, MemoryError):
        pass
    return None


def main():
    rows = psql_json("SELECT id, decision_text FROM decisions WHERE decision_id IS NULL ORDER BY id")
    if not rows:
        print("  No rows to migrate.")
        return

    updated = 0
    for r in rows:
        parsed = parse_decision_text(r["decision_text"])
        if parsed:
            decision_id = str(parsed["id"])
            detail = str(parsed.get("detail", ""))
            psql(
                f"UPDATE decisions SET decision_id='{decision_id.replace(chr(39), chr(39)*2)}', "
                f"detail='{detail.replace(chr(39), chr(39)*2)}' "
                f"WHERE id={r['id']}"
            )
        else:
            # plain text — use as detail, leave decision_id NULL
            psql(
                f"UPDATE decisions SET detail='{r['decision_text'].replace(chr(39), chr(39)*2)}' "
                f"WHERE id={r['id']}"
            )
        updated += 1
        if updated % 10 == 0:
            print(f"  Migrated {updated}/{len(rows)}")

    print(f"  Done: {updated} rows migrated.")


if __name__ == "__main__":
    sys.exit(main())
