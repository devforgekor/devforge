#!/usr/bin/env python3
# Status: production
# Path: manual — devforge (bootstrap/ship/verify) · onmydoc (remote-setup/remote-receive/remote-verify)
"""SSOT golden backup: encrypt locally with age, ship ciphertext to onmydoc, verify integrity.

Architecture B (2026-09-28, unified with kv-backup.py): devforge encrypts with the
shared age recipient BEFORE shipping, so onmydoc receives ciphertext only — it is an
untrusted relay with no keys. [WHY] one-way push (never sync): a compromised backup
server must not be able to poison the source. identity (private key) lives only on
the operator's local PC.

Usage:
  backup_ssot_golden.py bootstrap
  backup_ssot_golden.py ship [--dry-run] [--oci]
  backup_ssot_golden.py verify [--file NAME]
  backup_ssot_golden.py remote-setup
  backup_ssot_golden.py remote-receive --file NAME
  backup_ssot_golden.py remote-verify [--file NAME]

Contract: human logs -> stderr, single JSON line -> stdout.
Exit codes: verify 0=pass, 1=integrity mismatch, 2=error; other commands 0 ok / 2 error.
Restore rehearsal runs on the identity-holding PC:
  age -d -i <identity> <archive>.age > art.tar.gz && tar xz ... && manifest 대조
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shlex
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/opt/projects/server")
STAGE = Path("/opt/ai_data/backups/ssot-golden")
LOCAL_KEEP_DAYS = 7
REMOTE_KEEP = 30
SSH_HOST = "onmydoc"
REMOTE_DIR = "ssot-golden"
MANIFEST_NAME = "manifest.json"

SSOT_SOURCES: list[tuple[Path, list[str]]] = [
    (
        REPO,
        [
            "CLAUDE.md",
            "handover.yaml",
            "README.md",
            "docs/architecture/code-structure.yaml",
            "docs/domain-glossary.yaml",
            "docs/handover-secrets-kv.md",
            "docs/INDEX.md",
            "docs/CONVENTIONS.md",
            "docs/specs/*",
            "docs/adr/*",
            "docs/plans/*",
            "specs/*",
        ],
    ),
    (Path("/home/opc"), ["AGENTS.md", "CLAUDE.md"]),
]

RDIR = Path.home() / REMOTE_DIR
INBOX = RDIR / "inbox"
ARCHIVE = RDIR / "archive"
# [WHY] shared with kv-backup.py: one recipient on the server, one identity on the
# operator's local PC decrypts every backup kind (SSOT golden + secrets).
AGE_BIN = Path.home() / ".local" / "bin" / "age"
AGE_RECIPIENT_FILE = Path.home() / ".config" / "devforge" / "backup-age-recipient"


class BackupError(Exception):
    """Operational failure with a user-facing message."""


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    log(f"$ {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    except OSError as e:
        raise BackupError(f"cannot execute {cmd[0]}: {e}") from e
    if r.returncode != 0:
        log(f"FAILED rc={r.returncode}: {(r.stderr or '').strip()[:400]}")
    return r


def _relay_stderr(r: subprocess.CompletedProcess[str]) -> None:
    # [WHY] remote trace is useful on success; on failure run() already logged the
    # first 400 chars, so a raw relay would duplicate the FAILED line.
    if r.returncode == 0 and r.stderr:
        print(r.stderr, file=sys.stderr, end="")


def _safe_name(name: str) -> str:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise BackupError(f"unsafe file name: {name!r}")
    return name


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def collect() -> tuple[dict[str, Path], list[str]]:
    files: dict[str, Path] = {}
    unmatched: list[str] = []
    for root, patterns in SSOT_SOURCES:
        for pattern in patterns:
            full = root / pattern
            if _is_glob(pattern):
                matches = sorted(m for m in full.parent.glob(full.name) if m.is_file())
                if not matches:
                    unmatched.append(str(full))
                    continue
            elif full.is_file():
                matches = [full]
            else:
                raise BackupError(f"SSOT file missing: {full}")
            for m in matches:
                files[str(m).lstrip("/")] = m
    if not files:
        raise BackupError("no SSOT files collected")
    return files, unmatched


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_commit() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
        )
    except OSError:
        return "unknown"
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def build_manifest(files: dict[str, Path], unmatched: list[str], commit: str) -> dict:
    return {
        "schema": 1,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_commit": commit,
        "files": [
            {"path": rel, "sha256": sha256_file(p), "size": p.stat().st_size}
            for rel, p in sorted(files.items())
        ],
        "unmatched": unmatched,
    }


def stage(commit: str | None = None) -> tuple[Path, dict]:
    files, unmatched = collect()
    manifest = build_manifest(files, unmatched, commit if commit is not None else git_commit())
    STAGE.mkdir(parents=True, exist_ok=True)
    STAGE.chmod(0o700)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tar_path = STAGE / f"ssot-golden-{ts}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        blob = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(blob)
        tf.addfile(info, io.BytesIO(blob))
        for rel, p in sorted(files.items()):
            tf.add(p, arcname=rel, recursive=False)
    log(f"staged {tar_path.name} ({tar_path.stat().st_size} bytes, {len(files)} files)")
    tar_path.chmod(0o600)
    return tar_path, manifest


def prune_local_stage() -> int:
    cutoff = time.time() - LOCAL_KEEP_DAYS * 86400
    removed = 0
    for f in STAGE.glob("ssot-golden-*.tar.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed


def _parse_last_json(text: str) -> dict | None:
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _remote_cmd(script: str, subcmd: str, extra: str = "") -> str:
    return f"python3 {REMOTE_DIR}/{script} {subcmd} {extra}".rstrip()


def _scp_check(r: subprocess.CompletedProcess[str], what: str) -> None:
    if r.returncode != 0:
        raise BackupError(f"scp {what} failed: {(r.stderr or '').strip()[:300]}")


def bootstrap(_args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    # [WHY] first-ever bootstrap: scp cannot create the remote directory itself.
    r = run(["ssh", SSH_HOST, f"mkdir -p {REMOTE_DIR}"])
    if r.returncode != 0:
        raise BackupError(f"ssh {SSH_HOST} unreachable: {(r.stderr or '').strip()[:300]}")
    r = run(["scp", str(script), f"{SSH_HOST}:{REMOTE_DIR}/"])
    _scp_check(r, "script deploy")
    r = run(["ssh", SSH_HOST, _remote_cmd(script.name, "remote-setup")])
    if r.returncode != 0:
        raise BackupError(f"remote-setup failed: {(r.stderr or '').strip()[:300]}")
    _relay_stderr(r)
    remote = _parse_last_json(r.stdout)
    print(json.dumps({"command": "bootstrap", "status": "ok", "remote": remote}))
    return 0


def encrypt_local(tar_path: Path) -> Path:
    """[WHY] encrypt before shipping: plaintext never leaves this host, and the
    backup server stays keyless (untrusted relay). Recipient only — identity is
    on the operator's local PC."""
    if not AGE_RECIPIENT_FILE.is_file():
        raise BackupError(f"age recipient missing: {AGE_RECIPIENT_FILE}")
    recipient = AGE_RECIPIENT_FILE.read_text(encoding="utf-8").strip()
    age_path = Path(str(tar_path) + ".age")
    r = run([str(AGE_BIN), "-r", recipient, "-o", str(age_path), str(tar_path)])
    if r.returncode != 0 or not age_path.is_file():
        raise BackupError(f"age encrypt failed: {(r.stderr or '').strip()[:300]}")
    age_path.chmod(0o600)
    log(f"encrypted {age_path.name} ({age_path.stat().st_size} bytes)")
    return age_path


def write_sidecar(age_path: Path) -> Path:
    sidecar = Path(str(age_path) + ".sha256")
    sidecar.write_text(f"{sha256_file(age_path)}  {age_path.name}\n", encoding="utf-8")
    sidecar.chmod(0o600)
    return sidecar


def ship(args: argparse.Namespace) -> int:
    tar_path, manifest = stage()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "command": "ship",
                    "status": "dry-run",
                    "archive": tar_path.name,
                    "files": len(manifest["files"]),
                    "unmatched": manifest["unmatched"],
                    "next": [
                        "age encrypt",
                        f"scp -> {SSH_HOST}:{REMOTE_DIR}/inbox/",
                        "remote-receive",
                    ],
                }
            )
        )
        return 0
    age_path = encrypt_local(tar_path)
    sidecar = write_sidecar(age_path)
    script = Path(__file__).resolve()
    r = run(["scp", str(script), f"{SSH_HOST}:{REMOTE_DIR}/"])
    _scp_check(r, "script deploy (run bootstrap first?)")
    r = run(["scp", str(age_path), str(sidecar), f"{SSH_HOST}:{REMOTE_DIR}/inbox/"])
    _scp_check(r, f"archive {age_path.name}")
    r = run(
        ["ssh", SSH_HOST, _remote_cmd(script.name, "remote-receive", f"--file {age_path.name}")]
    )
    if r.returncode != 0:
        raise BackupError(f"remote-receive failed: {(r.stderr or '').strip()[:300]}")
    _relay_stderr(r)
    remote = _parse_last_json(r.stdout)
    result: dict = {"command": "ship", "status": "ok", "archive": age_path.name, "remote": remote}
    if args.oci:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import osync_backup  # noqa: PLC0415 — lazy so plain ship never touches the oci CLI

        try:
            osync_backup.oci_upload(age_path, f"backups/ssot-golden/{age_path.name}")
        except RuntimeError as e:
            raise BackupError(f"oci upload failed: {e}") from e
    pruned = prune_local_stage()
    result["local_pruned"] = pruned
    print(json.dumps(result))
    return 0


def verify(args: argparse.Namespace) -> int:
    extra = f"--file {shlex.quote(args.file)}" if args.file else ""
    r = run(["ssh", SSH_HOST, _remote_cmd("backup_ssot_golden.py", "remote-verify", extra)])
    if r.stdout:
        print(r.stdout, end="")
    _relay_stderr(r)
    # [WHY] ssh uses 255 for transport failures; the contract reserves 0/1/2.
    return 2 if r.returncode == 255 else r.returncode


def remote_setup(_args: argparse.Namespace) -> int:
    for d in (INBOX, ARCHIVE, RDIR / "logs"):
        d.mkdir(parents=True, exist_ok=True)
        # [WHY] ciphertext-only relay — inbox/logs are owner-only; no keys ever live here.
        d.chmod(0o700)
    result = {
        "command": "remote-setup",
        "status": "ok",
        "mode": "keyless relay (ciphertext receive + sha256 verify)",
    }
    print(json.dumps(result))
    return 0


def prune_archive() -> int:
    archives = sorted(ARCHIVE.glob("*.age"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in archives[REMOTE_KEEP:]:
        stale.unlink()
        Path(str(stale) + ".sha256").unlink(missing_ok=True)
        removed += 1
    return removed


def remote_receive(args: argparse.Namespace) -> int:
    inbox_file = INBOX / _safe_name(args.file)
    if not inbox_file.is_file():
        raise BackupError(f"inbox file missing: {inbox_file}")
    sidecar = INBOX / (inbox_file.name + ".sha256")
    if not sidecar.is_file():
        raise BackupError(f"sidecar missing: {sidecar}")
    expected = sidecar.read_text(encoding="utf-8").split()[0]
    actual = sha256_file(inbox_file)
    if expected != actual:
        # [WHY] corrupt ciphertext stays in inbox for diagnosis; next ship overwrites it.
        raise BackupError(f"ciphertext sha256 mismatch: expected {expected} got {actual}")
    dest = ARCHIVE / inbox_file.name
    inbox_file.replace(dest)
    sidecar.replace(ARCHIVE / sidecar.name)
    removed = prune_archive()
    print(
        json.dumps(
            {
                "command": "remote-receive",
                "status": "ok",
                "archive": dest.name,
                "sha256": actual,
                "pruned": removed,
            }
        )
    )
    return 0


def newest_archive() -> Path | None:
    archives = sorted(ARCHIVE.glob("*.age"), key=lambda p: p.stat().st_mtime)
    return archives[-1] if archives else None


def remote_verify(args: argparse.Namespace) -> int:
    archive = ARCHIVE / _safe_name(args.file) if args.file else newest_archive()
    if archive is None or not archive.is_file():
        print(json.dumps({"command": "remote-verify", "status": "error", "error": "no archive"}))
        return 2
    sidecar = Path(str(archive) + ".sha256")
    if not sidecar.exists():
        print(
            json.dumps(
                {
                    "command": "remote-verify",
                    "archive": archive.name,
                    "status": "error",
                    "error": "sidecar missing",
                }
            )
        )
        return 2
    try:
        expected = sidecar.read_text(encoding="utf-8").split()[0]
    except OSError as e:
        print(
            json.dumps(
                {
                    "command": "remote-verify",
                    "archive": archive.name,
                    "status": "error",
                    "error": f"sidecar unreadable: {e}",
                }
            )
        )
        return 2
    # [WHY] the relay holds no identity, so it can only prove ciphertext integrity;
    # content/manifest verification is a restore rehearsal on the identity-holding PC.
    with open(archive, "rb") as f:
        if f.read(21) != b"age-encryption.org/v1":
            print(
                json.dumps(
                    {
                        "command": "remote-verify",
                        "archive": archive.name,
                        "status": "error",
                        "error": "not an age ciphertext file",
                    }
                )
            )
            return 2
    actual = sha256_file(archive)
    if expected != actual:
        print(
            json.dumps(
                {
                    "command": "remote-verify",
                    "archive": archive.name,
                    "status": "fail",
                    "expected": expected,
                    "actual": actual,
                }
            )
        )
        return 1
    print(
        json.dumps(
            {
                "command": "remote-verify",
                "archive": archive.name,
                "status": "pass",
                "sha256": actual,
                "note": "ciphertext integrity only; restore rehearsal on identity PC",
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="backup_ssot_golden", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap").set_defaults(func=bootstrap)
    p = sub.add_parser("ship")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--oci", action="store_true")
    p.set_defaults(func=ship)
    p = sub.add_parser("verify")
    p.add_argument("--file")
    p.set_defaults(func=verify)
    sub.add_parser("remote-setup").set_defaults(func=remote_setup)
    p = sub.add_parser("remote-receive")
    p.add_argument("--file", required=True)
    p.set_defaults(func=remote_receive)
    p = sub.add_parser("remote-verify")
    p.add_argument("--file")
    p.set_defaults(func=remote_verify)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (BackupError, OSError) as e:
        print(json.dumps({"command": args.command, "status": "error", "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
