"""Shared PostgreSQL helpers for DevForge scripts.

Usage:
    from lib.db import psql, psql_ok, esc_sql, db_table_exists, db_row_exists

    rows = psql("SELECT * FROM turns LIMIT 5")
    ok = psql_ok("INSERT INTO ...")
    safe = esc_sql("O'Reilly\nquote")
    has_turns = db_table_exists("turns")
    has_pgvector = db_row_exists("SELECT 1 FROM pg_extension WHERE extname='vector'")
"""

import subprocess
from typing import Optional

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
        "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]

# Non-interactive psql (no stdin pipe) for boolean checks
PSQL_CHECK = ["podman", "exec", "postgres", "psql", "-U", "postgres",
              "-d", "devforge_app", "-t"]


def psql(sql: str, timeout: int = 30) -> str:
    """Execute SQL, return stripped stdout. Returns empty string on error."""
    try:
        r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return ""


def psql_ok(sql: str, timeout: int = 30) -> bool:
    """Execute SQL, return True if statement succeeded."""
    try:
        r = subprocess.run(PSQL + ["-c", sql], capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print(f"  SQL ERROR: {r.stderr.strip()[:200]}")
        return r.returncode == 0
    except Exception as e:
        print(f"  SQL ERROR: {e}")
        return False


def db_table_exists(table: str) -> bool:
    try:
        r = subprocess.run(
            PSQL_CHECK + ["-c", f"SELECT 1 FROM pg_tables WHERE tablename='{table}'"],
            capture_output=True, text=True, timeout=10,
        )
        return "1" in r.stdout
    except Exception:
        return False


def db_row_exists(sql: str) -> bool:
    try:
        r = subprocess.run(
            PSQL_CHECK + ["-c", sql],
            capture_output=True, text=True, timeout=10,
        )
        return "1" in r.stdout
    except Exception:
        return False


def esc_sql(s: str) -> str:
    """Escape string for safe SQL literal interpolation."""
    return s.replace("\\", "\\\\").replace("'", "''").replace("\n", " ").replace("\r", " ")


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
