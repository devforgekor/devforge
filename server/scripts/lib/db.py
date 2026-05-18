"""Shared PostgreSQL helpers for DevForge scripts.

Usage:
    from lib.db import psql, psql_ok, esc_sql

    rows = psql("SELECT * FROM turns LIMIT 5")
    ok = psql_ok("INSERT INTO ...")
    safe = esc_sql("O'Reilly\nquote")
"""

import subprocess

PSQL = ["podman", "exec", "-i", "postgres", "psql", "-U", "postgres",
        "-d", "devforge_app", "--no-align", "--tuples-only", "--quiet"]


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


def esc_sql(s: str) -> str:
    """Escape string for safe SQL literal interpolation."""
    return s.replace("\\", "\\\\").replace("'", "''").replace("\n", " ").replace("\r", " ")
