#!/usr/bin/env python3.11
# Status: production
# Path: systemd --user: devforge-backup.service -> osync_backup.py (daily)
"""DevForge backup -> OCI Object Storage (bucket devforge-standard).

Targets:
  backups/database/    pg_dump -Fc (custom, compressed)   daily
  backups/application/ code + docs + systemd units (tgz)  weekly

Usage:
  osync_backup.py db [--force] [--dry-run]
  osync_backup.py app [--dry-run]
  osync_backup.py all [--force] [--dry-run]   # db always, app once per ISO week

Retention:
  remote: database 30d, application 56d
  local : database 7d,  application 4d
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BUCKET = "devforge-standard"
OCI = "/home/opc/.local/bin/oci"
STAGE = Path("/opt/ai_data/backups")
DB_DIR = STAGE / "db"
APP_DIR = STAGE / "app"
LOG = STAGE / "osync.log"

DB_PREFIX = "backups/database/"
APP_PREFIX = "backups/application/"
DB_REMOTE_KEEP = 30
DB_LOCAL_KEEP = 7
APP_REMOTE_KEEP = 56
APP_LOCAL_KEEP = 4

APP_SOURCES = [
    ("/opt/projects/server/scripts", "scripts"),
    ("/opt/projects/server/docs", "docs"),
    ("/home/opc/.config/systemd/user", "systemd-user"),
]
APP_EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".ruff_cache", ".pytest_cache",
    ".mypy_cache", "_archive", "_backup", ".venv", "dist", "build", ".cache",
}
APP_EXCLUDE_SUFFIX = (".pyc", ".pyo", ".env", ".pem", ".key")


def log(msg: str) -> None:
    utc_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{utc_timestamp}] {msg}"
    print(line, flush=True)
    try:
        STAGE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def oci(args: list[str]) -> dict:
    env = dict(os.environ, SUPPRESS_LABEL_WARNING="True")
    r = subprocess.run([OCI, *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"oci {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout) if r.stdout.strip() else {}


def oci_upload(local: Path, name: str) -> None:
    log(f"upload {local.name} ({local.stat().st_size / 1e6:.1f} MB) -> {BUCKET}/{name}")
    oci(["os", "object", "put", "--bucket-name", BUCKET, "--name", name,
         "--file", str(local), "--force", "--part-size", "128"])


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def prune_remote(prefix: str, keep_days: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    data = oci(["os", "object", "list", "--bucket-name", BUCKET, "--prefix", prefix,
                "--fields", "name,timeCreated", "--all"])
    for o in data.get("data", []):
        name = o.get("name", "")
        if name.endswith("/"):
            continue
        dt = _parse_ts(o.get("time-created", ""))
        if dt and dt < cutoff:
            log(f"prune remote {name}")
            try:
                oci(["os", "object", "delete", "--bucket-name", BUCKET,
                     "--object-name", name, "--force"])
            except RuntimeError as e:
                log(f"prune remote failed: {e}")


def prune_local(dir_: Path, keep_days: int, pattern: str) -> None:
    cutoff = time.time() - keep_days * 86400
    for f in dir_.glob(pattern):
        if f.stat().st_mtime < cutoff:
            log(f"prune local {f.name}")
            f.unlink(missing_ok=True)


def db_backup(force: bool, dry: bool) -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sentinel = STAGE / f".db_done_{day}"
    out = DB_DIR / f"devforge_{day}.dump"
    if sentinel.exists() and not force:
        log(f"db: already done today ({sentinel.name}) - skip")
        return 0
    if dry:
        log(f"db: [dry-run] pg_dump -> {out}, upload {DB_PREFIX}{out.name}")
        return 0
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as fh:
        p = subprocess.Popen(
            ["podman", "exec", "postgres", "pg_dump", "-U", "devforge", "-Fc", "devforge_app"],
            stdout=fh, stderr=subprocess.PIPE,
        )
        _, err = p.communicate()
    if p.returncode != 0:
        out.unlink(missing_ok=True)
        log(f"db: pg_dump failed: {(err or b'').decode(errors='ignore')[:300]}")
        return 1
    if out.stat().st_size < 1024:
        out.unlink(missing_ok=True)
        log("db: dump too small, abort")
        return 1
    log(f"db: dumped {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    oci_upload(out, DB_PREFIX + out.name)
    sentinel.write_text(datetime.now(timezone.utc).isoformat())
    prune_remote(DB_PREFIX, DB_REMOTE_KEEP)
    prune_local(DB_DIR, DB_LOCAL_KEEP, "*.dump")
    return 0


def _tar_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(ti.name).parts
    if any(p in APP_EXCLUDE_DIRS for p in parts):
        return None
    name = Path(ti.name).name
    if name.endswith(APP_EXCLUDE_SUFFIX) or name == ".env":
        return None
    if "secret" in name.lower() or "credential" in name.lower():
        return None
    return ti


def app_backup(dry: bool) -> int:
    now = datetime.now(timezone.utc)
    wk = f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    sentinel = STAGE / f".app_done_{wk}"
    out = APP_DIR / f"devforge_app_{now.strftime('%Y-%m-%d')}.tgz"
    if sentinel.exists():
        log(f"app: already done this week ({sentinel.name}) - skip")
        return 0
    if dry:
        log(f"app: [dry-run] tar {APP_SOURCES} -> {out}, upload {APP_PREFIX}{out.name}")
        return 0
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w:gz") as tar:
        for src, arc in APP_SOURCES:
            p = Path(src)
            if p.exists():
                tar.add(src, arcname=arc, filter=_tar_filter)
    log(f"app: archived {out.name} ({out.stat().st_size / 1e6:.1f} MB)")
    oci_upload(out, APP_PREFIX + out.name)
    sentinel.write_text(now.isoformat())
    prune_remote(APP_PREFIX, APP_REMOTE_KEEP)
    prune_local(APP_DIR, APP_LOCAL_KEEP, "*.tgz")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DevForge -> OCI Object Storage backup")
    ap.add_argument("mode", choices=["db", "app", "all"], nargs="?", default="db")
    ap.add_argument("--force", action="store_true", help="ignore daily sentinel")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    STAGE.mkdir(parents=True, exist_ok=True)
    rc = 0
    if a.mode in ("db", "all"):
        rc |= db_backup(a.force, a.dry_run)
    if a.mode == "app" or (a.mode == "all" and not (STAGE / f".app_done_"
                            f"{datetime.now(timezone.utc).isocalendar().year}-W"
                            f"{datetime.now(timezone.utc).isocalendar().week:02d}").exists()):
        rc |= app_backup(a.dry_run)
    return rc


if __name__ == "__main__":
    sys.exit(main())
