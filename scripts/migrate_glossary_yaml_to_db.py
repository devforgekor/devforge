#!/usr/bin/env python3
# Status: production
# Path: manual — one-shot migration
"""One-shot: migrate domain-glossary.yaml → bounded_contexts + glossary_terms tables."""
import yaml, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.db import psql_ok, esc_sql

GLOSSARY_PATH = Path("/opt/projects/server/docs/domain-glossary.yaml")

def main():
    raw = GLOSSARY_PATH.read_text()
    if raw.startswith("#"):
        _, _, raw = raw.partition("\n")
    data = yaml.safe_load(raw)
    if not data or "bounded_contexts" not in data:
        print("ERROR: domain-glossary.yaml empty or invalid")
        sys.exit(1)

    ctx_count = 0
    term_count = 0

    for bc in data["bounded_contexts"]:
        bc_id = int(bc["id"])
        bc_name = esc_sql(bc["name"])
        ok = psql_ok(f"INSERT INTO bounded_contexts (id, name) VALUES ({bc_id}, '{bc_name}') ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name")
        if ok:
            ctx_count += 1
            print(f"  Context OK: {bc['name']}")
        else:
            print(f"  Context FAIL: {bc['name']}")

        for term_entry in bc.get("terms", []):
            term = esc_sql(term_entry["term"])
            definition = esc_sql(term_entry["definition"])
            tables = term_entry.get("tables", [])
            files = term_entry.get("related_files", [])
            tables_pg = "'{}'::text[]" if not tables else "ARRAY[" + ", ".join(f"'{esc_sql(t)}'" for t in tables) + "]"
            files_pg = "'{}'::text[]" if not files else "ARRAY[" + ", ".join(f"'{esc_sql(f)}'" for f in files) + "]"

            sql = f"""INSERT INTO glossary_terms (term, definition, bounded_context_id, tables_ref, related_files)
            VALUES ('{term}', '{definition}', {bc_id}, {tables_pg}, {files_pg})
            ON CONFLICT (term, bounded_context_id) DO UPDATE SET
                definition = EXCLUDED.definition,
                tables_ref = EXCLUDED.tables_ref,
                related_files = EXCLUDED.related_files"""
            ok = psql_ok(sql)
            if ok:
                term_count += 1
            else:
                print(f"  Term FAIL: {term_entry['term']}")

    print(f"\nMigrated {ctx_count} contexts, {term_count} terms")

if __name__ == "__main__":
    main()
