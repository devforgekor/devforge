#!/usr/bin/env python3
# Status: production
# Path: imported by — production scripts
"""Shared PostgreSQL helpers for DevForge scripts.

Usage:
    from lib.db import psql, psql_ok, esc_sql, db_table_exists, db_row_exists

    rows = psql("SELECT * FROM turns LIMIT 5")
    ok = psql_ok("INSERT INTO ...")
    safe = esc_sql("O'Reilly\nquote")
    has_turns = db_table_exists("turns")
    has_pgvector = db_row_exists("SELECT 1 FROM pg_extension WHERE extname='vector'")
"""

import os
import subprocess
from typing import Optional

# Container TCP mode: use psql -h 127.0.0.1 instead of podman exec
if os.environ.get("DEVFORGE_DB_TCP"):
    PSQL = [
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        "postgres",
        "-d",
        "devforge_app",
        "--no-align",
        "--tuples-only",
        "--quiet",
    ]
    PSQL_CHECK = ["psql", "-h", "127.0.0.1", "-U", "postgres", "-d", "devforge_app", "-t"]
else:
    PSQL = [
        "podman",
        "exec",
        "-i",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "devforge_app",
        "--no-align",
        "--tuples-only",
        "--quiet",
    ]
    PSQL_CHECK = [
        "podman",
        "exec",
        "postgres",
        "psql",
        "-U",
        "postgres",
        "-d",
        "devforge_app",
        "-t",
    ]


def psql(sql: str, timeout: int = 30) -> str:
    """Execute SQL via stdin (-f -), return stripped stdout. Empty on error."""
    try:
        r = subprocess.run(
            PSQL + ["-f", "-"], input=sql, capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return ""


def psql_json(sql: str, timeout: int = 30) -> list[dict]:
    """Execute SQL with row_to_json wrapping, return list of dicts.

    Wraps the query in SELECT row_to_json(r) FROM (...) r so column values
    containing ``|`` do not break parsing (unlike psql() pipe-delimited output).
    """
    import json as _json

    wrapped = f"SELECT row_to_json(r) FROM ({sql}) r"
    try:
        r = subprocess.run(
            PSQL + ["-f", "-"], input=wrapped, capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
            return []
        raw = r.stdout.strip()
        if not raw:
            return []
        result = []
        for line in raw.split("\n"):
            line = line.strip()
            if line:
                result.append(_json.loads(line))
        return result
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return []


def psql_ok(sql: str, timeout: int = 30) -> bool:
    """Execute SQL via stdin (-f -), return True if statement succeeded."""
    try:
        r = subprocess.run(
            PSQL + ["-f", "-"], input=sql, capture_output=True, text=True, timeout=timeout
        )
        if r.returncode != 0:
            print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
        return r.returncode == 0
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return False


def db_table_exists(table: str) -> bool:
    try:
        r = subprocess.run(
            PSQL_CHECK + ["-c", f"SELECT 1 FROM pg_tables WHERE tablename='{esc_sql(table)}'"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "1" in r.stdout
    except Exception:
        return False


def db_row_exists(sql: str) -> bool:
    try:
        r = subprocess.run(
            PSQL_CHECK + ["-c", sql],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return "1" in r.stdout
    except Exception:
        return False


def escape_sql_string(s: str) -> str:
    """Escape string for safe SQL literal interpolation."""
    return (
        s.replace("\x00", "")
        .replace("\\", "\\\\")
        .replace("'", "''")
        .replace("\n", " ")
        .replace("\r", " ")
    )


esc_sql = (
    escape_sql_string  # alias for backward compatibility; new callers should use escape_sql_string
)


def get_token_stats() -> Optional[dict]:
    """Return session + all-time token stats from turns table (shared CTE)."""
    rows = psql(
        """
        WITH latest_conv AS (
            SELECT conversation_id AS id
            FROM turns
            WHERE meta ? 'tokens'
            ORDER BY created_at DESC
            LIMIT 1
        ),
        session_period AS (
            SELECT COUNT(*) AS turns,
                   COALESCE(SUM((t.meta->>'tokens')::int), 0) AS total_tokens,
                   COALESCE(AVG((t.meta->>'tokens')::numeric), 0) AS avg_tokens
            FROM turns t
            JOIN latest_conv lc ON t.conversation_id = lc.id
            WHERE t.meta ? 'tokens'
        ),
        total_period AS (
            SELECT COUNT(*) AS turns,
                   COALESCE(SUM((meta->>'tokens')::int), 0) AS total_tokens,
                   COALESCE(AVG((meta->>'tokens')::numeric), 0) AS avg_tokens
            FROM turns
            WHERE meta ? 'tokens'
        )
        SELECT session_period.turns,
               session_period.total_tokens,
               ROUND(session_period.avg_tokens, 0)::int AS avg_tokens,
               total_period.turns,
               total_period.total_tokens,
               ROUND(total_period.avg_tokens, 0)::int AS total_avg_tokens
        FROM session_period
        CROSS JOIN total_period
        """
    )
    if rows and "|" in rows:
        parts = rows.split("|")
        if len(parts) >= 6:
            try:
                return {
                    "session": {
                        "turns": int(parts[0]),
                        "total_tokens": int(parts[1]),
                        "avg_tokens": int(parts[2]),
                    },
                    "total": {
                        "turns": int(parts[3]),
                        "total_tokens": int(parts[4]),
                        "avg_tokens": int(parts[5]),
                    },
                }
            except ValueError:
                pass
    return None


def get_checkpoint(phase: str) -> str:
    """Return max_created_at from pipeline_checkpoint for given phase."""
    row = psql(f"SELECT max_created_at::text FROM pipeline_checkpoint WHERE phase = '{phase}'")
    return row or "-infinity"


def advance_checkpoint(phase: str, created_at_str: str):
    """Advance checkpoint to created_at if newer. SSOT: turns.created_at."""
    cs = esc_sql(created_at_str)
    psql_ok(
        f"UPDATE pipeline_checkpoint "
        f"SET max_created_at = '{cs}'::timestamptz, "
        f"    updated_at = NOW() "
        f"WHERE phase = '{phase}' "
        f"  AND max_created_at < '{cs}'::timestamptz"
    )
