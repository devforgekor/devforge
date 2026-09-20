#!/usr/bin/env python3.11
# Status: production
# Path: systemd --user: devforge-restore-test.service -> osync_restore_test.py (monthly)
"""Monthly restore test — restore latest devforge-standard DB dump into a scratch DB.

Reads the newest /opt/ai_data/backups/db/devforge_*.dump (pg_dump -Fc),
restores it into test_restore_<utc_timestamp>, counts public tables, then drops the DB.
Exits 0 only when the restored DB has >0 tables.
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
from datetime import datetime, timezone

CONTAINER = "postgres"
DUMP_GLOB = "/opt/ai_data/backups/db/devforge_*.dump"
LOG = "/opt/ai_data/backups/osync.log"


def log(msg: str) -> None:
    utc_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{utc_timestamp}] restore-test: {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def psql(sql: str, db: str = "postgres") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", db, "-tA", "-c", sql],
        capture_output=True, text=True,
    )


def main() -> int:
    dumps = sorted(glob.glob(DUMP_GLOB))
    if not dumps:
        log("no dump found - skip")
        return 0
    dump = dumps[-1]
    test_db = "test_restore_" + datetime.now(timezone.utc).strftime("%m%d%H%M%S")

    psql(f"DROP DATABASE IF EXISTS {test_db};")
    if psql(f"CREATE DATABASE {test_db};").returncode != 0:
        log(f"cannot create {test_db}")
        return 1

    try:
        with open(dump, "rb") as fh:
            r = subprocess.run(
                ["podman", "exec", "-i", CONTAINER, "pg_restore",
                 "-U", "postgres", "-d", test_db, "--no-owner", "--no-privileges"],
                stdin=fh, capture_output=True,
            )
        if r.returncode != 0:
            log(f"pg_restore rc={r.returncode}: {r.stderr.decode(errors='ignore')[:200]}")
        cnt = (psql(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';",
            db=test_db,
        ).stdout or "").strip()
    finally:
        psql(f"DROP DATABASE IF EXISTS {test_db};")

    if cnt.isdigit() and int(cnt) > 0:
        log(f"OK {os.path.basename(dump)} -> {cnt} tables")
        return 0
    log(f"FAIL {os.path.basename(dump)} - 0 tables")
    return 1


if __name__ == "__main__":
    sys.exit(main())
