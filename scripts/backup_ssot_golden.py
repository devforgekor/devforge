#!/usr/bin/env python3
# Status: production
# Path: manual — devforge (bootstrap/ship/verify) · onmydoc (remote-setup/remote-receive/remote-verify)
"""SSOT golden backup: ship a plain tar to onmydoc, encrypt (age->gpg) and verify there.

DevForge never runs gpg/age — encryption, keys, and verification live only on the
backup server (onmydoc), so plaintext and private keys never share one host.

Usage:
  backup_ssot_golden.py bootstrap
  backup_ssot_golden.py ship [--dry-run] [--oci]
  backup_ssot_golden.py verify [--file NAME]
  backup_ssot_golden.py remote-setup
  backup_ssot_golden.py remote-receive --file NAME
  backup_ssot_golden.py remote-verify [--file NAME]

Contract: human logs -> stderr, single JSON line -> stdout.
Exit codes: verify 0=pass, 1=manifest mismatch, 2=error; other commands 0 ok / 2 error.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/opt/projects/server")
STAGE = Path("/opt/ai_data/backups/ssot-golden")
LOCAL_KEEP_DAYS = 7
REMOTE_KEEP = 30
SSH_HOST = "onmydoc"
REMOTE_DIR = "ssot-golden"
GPG_ID = "DevForge SSOT Golden <ssot-golden@devforge.invalid>"
MANIFEST_NAME = "manifest.json"
# [WHY] age releases publish no sha256 checksums (minisign .proof only), so
# integrity of the install is established via GitHub HTTPS + `age --version`.
AGE_VERSION = "v1.3.2"

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
KEYS = RDIR / "keys"
GNUPG = KEYS / "gnupg"
AGE_KEY = KEYS / "age-key.txt"
AGE_BIN = Path.home() / ".local" / "bin" / "age"
AGE_KEYGEN = Path.home() / ".local" / "bin" / "age-keygen"


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
                    "next": [f"scp -> {SSH_HOST}:{REMOTE_DIR}/inbox/", "remote-receive"],
                }
            )
        )
        return 0
    script = Path(__file__).resolve()
    r = run(["scp", str(script), f"{SSH_HOST}:{REMOTE_DIR}/"])
    _scp_check(r, "script deploy (run bootstrap first?)")
    r = run(["scp", str(tar_path), f"{SSH_HOST}:{REMOTE_DIR}/inbox/"])
    _scp_check(r, f"archive {tar_path.name}")
    r = run(
        ["ssh", SSH_HOST, _remote_cmd(script.name, "remote-receive", f"--file {tar_path.name}")]
    )
    if r.returncode != 0:
        raise BackupError(f"remote-receive failed: {(r.stderr or '').strip()[:300]}")
    _relay_stderr(r)
    remote = _parse_last_json(r.stdout)
    result: dict = {"command": "ship", "status": "ok", "archive": tar_path.name, "remote": remote}
    if args.oci:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import osync_backup  # noqa: PLC0415 — lazy so plain ship never touches the oci CLI

        try:
            osync_backup.oci_upload(tar_path, f"backups/ssot-golden/{tar_path.name}")
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


def install_age() -> None:
    if AGE_BIN.is_file() and AGE_KEYGEN.is_file():
        r = run([str(AGE_BIN), "--version"])
        if r.returncode == 0:
            return
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine().lower())
    if arch is None:
        raise BackupError(f"unsupported arch for age: {platform.machine()}")
    url = (
        f"https://github.com/FiloSottile/age/releases/download/"
        f"{AGE_VERSION}/age-{AGE_VERSION}-linux-{arch}.tar.gz"
    )
    AGE_BIN.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with (
            urllib.request.urlopen(url, timeout=60) as resp,
            tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp,
        ):
            tmp_path = Path(tmp.name)
            shutil.copyfileobj(resp, tmp)
        with tarfile.open(tmp_path) as tf:
            for name in ("age/age", "age/age-keygen"):
                src = tf.extractfile(name)
                if src is None:
                    raise BackupError(f"age release member missing: {name}")
                dest = AGE_BIN.parent / Path(name).name
                dest.write_bytes(src.read())
                dest.chmod(0o755)
    except BackupError:
        raise
    except (OSError, tarfile.TarError) as e:
        raise BackupError(f"age download/install failed: {e}") from e
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    r = run([str(AGE_BIN), "--version"])
    if r.returncode != 0:
        raise BackupError("age install failed --version check")


def gen_gpg_key() -> None:
    GNUPG.mkdir(parents=True, exist_ok=True)
    GNUPG.chmod(0o700)
    common = [
        "--batch",
        "--yes",
        "--pinentry-mode",
        "loopback",
        "--passphrase",
        "",
        "--homedir",
        str(GNUPG),
    ]
    r = run(["gpg", *common, "--quick-generate-key", GPG_ID, "ed25519", "cert", "never"])
    if r.returncode != 0:
        raise BackupError(f"gpg keygen failed: {(r.stderr or '').strip()[:300]}")
    fp = gpg_fingerprint()
    r = run(["gpg", *common, "--quick-add-key", fp, "cv25519", "encr", "never"])
    if r.returncode != 0:
        raise BackupError(f"gpg subkey failed: {(r.stderr or '').strip()[:300]}")


def gen_age_key() -> None:
    r = run([str(AGE_KEYGEN), "-o", str(AGE_KEY)])
    if r.returncode != 0:
        raise BackupError(f"age-keygen failed: {(r.stderr or '').strip()[:300]}")
    AGE_KEY.chmod(0o600)


def gpg_fingerprint() -> str:
    r = run(["gpg", "--homedir", str(GNUPG), "--with-colons", "--list-secret-keys"])
    if r.returncode != 0:
        raise BackupError("gpg secret key list failed")
    for line in (r.stdout or "").splitlines():
        if line.startswith("fpr:"):
            parts = line.split(":")
            if len(parts) > 9 and parts[9]:
                return parts[9]
    raise BackupError("gpg fingerprint not found")


def age_recipient() -> str:
    if not AGE_KEY.is_file():
        raise BackupError(f"age key missing: {AGE_KEY}")
    for line in AGE_KEY.read_text(encoding="utf-8").splitlines():
        if line.startswith("# public key:"):
            return line.split(":", 1)[1].strip()
    raise BackupError("age public key not found in key file")


def remote_setup(_args: argparse.Namespace) -> int:
    for d in (INBOX, ARCHIVE, RDIR / "logs", KEYS):
        d.mkdir(parents=True, exist_ok=True)
        # [WHY] inbox/logs carry plaintext during receive/verify — owner-only.
        d.chmod(0o700)
    install_age()
    pk = GNUPG / "private-keys-v1.d"
    if not pk.is_dir() or not any(pk.iterdir()):
        gen_gpg_key()
    if not AGE_KEY.is_file():
        gen_age_key()
    result = {
        "command": "remote-setup",
        "status": "ok",
        "gpg_fingerprint": gpg_fingerprint(),
        "age_recipient": age_recipient(),
        "key_backup": f"copy {KEYS} (gnupg/ + age-key.txt) to your local PC now",
    }
    print(json.dumps(result))
    return 0


def prune_archive() -> int:
    archives = sorted(ARCHIVE.glob("*.age.gpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in archives[REMOTE_KEEP:]:
        stale.unlink()
        removed += 1
    return removed


def remote_receive(args: argparse.Namespace) -> int:
    inbox_file = INBOX / _safe_name(args.file)
    if not inbox_file.is_file():
        raise BackupError(f"inbox file missing: {inbox_file}")
    age_out = Path(str(inbox_file) + ".age")
    age_out.unlink(missing_ok=True)
    r = run([str(AGE_BIN), "-r", age_recipient(), "-o", str(age_out), str(inbox_file)])
    if r.returncode != 0:
        raise BackupError(f"age encrypt failed: {(r.stderr or '').strip()[:300]}")
    gpg_out = Path(str(age_out) + ".gpg")
    gpg_out.unlink(missing_ok=True)
    r = run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            str(GNUPG),
            "-r",
            gpg_fingerprint(),
            "--encrypt",
            "--output",
            str(gpg_out),
            str(age_out),
        ]
    )
    if r.returncode != 0:
        raise BackupError(f"gpg encrypt failed: {(r.stderr or '').strip()[:300]}")
    dest = ARCHIVE / gpg_out.name
    gpg_out.replace(dest)
    inbox_file.unlink()
    age_out.unlink()
    removed = prune_archive()
    print(
        json.dumps(
            {
                "command": "remote-receive",
                "status": "ok",
                "archive": dest.name,
                "pruned": removed,
            }
        )
    )
    return 0


def newest_archive() -> Path | None:
    archives = sorted(ARCHIVE.glob("*.age.gpg"), key=lambda p: p.stat().st_mtime)
    return archives[-1] if archives else None


def compare_manifest(extract_dir: Path, manifest: dict) -> dict:
    entries = manifest.get("files", [])
    listed = {e["path"] for e in entries}
    missing: list[str] = []
    changed: list[str] = []
    ok = 0
    for entry in entries:
        p = extract_dir / entry["path"]
        if not p.is_file():
            missing.append(entry["path"])
        elif sha256_file(p) != entry.get("sha256"):
            changed.append(entry["path"])
        else:
            ok += 1
    actual = {
        str(p.relative_to(extract_dir))
        for p in extract_dir.rglob("*")
        if p.is_file() and p.name != MANIFEST_NAME
    }
    extra = sorted(actual - listed)
    status = "pass" if not (missing or changed or extra) else "fail"
    return {
        "status": status,
        "checked": len(entries),
        "ok": ok,
        "missing": missing,
        "changed": changed,
        "extra": extra,
    }


def verify_extracted(extract_dir: Path) -> tuple[dict, int]:
    mf = extract_dir / MANIFEST_NAME
    if not mf.is_file():
        return {"status": "error", "error": "manifest.json missing"}, 2
    try:
        manifest = json.loads(mf.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"manifest unreadable: {e}"}, 2
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        return {"status": "error", "error": "manifest malformed"}, 2
    if any(
        not isinstance(e, dict) or not isinstance(e.get("path"), str) for e in manifest["files"]
    ):
        return {"status": "error", "error": "manifest entries malformed"}, 2
    report = compare_manifest(extract_dir, manifest)
    return report, (0 if report["status"] == "pass" else 1)


def remote_verify(args: argparse.Namespace) -> int:
    archive = ARCHIVE / _safe_name(args.file) if args.file else newest_archive()
    if archive is None or not archive.is_file():
        print(json.dumps({"command": "remote-verify", "status": "error", "error": "no archive"}))
        return 2
    logs = RDIR / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="verify-", dir=logs))
    try:
        age_ct = tmp / "artifact.age"
        r = run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--homedir",
                str(GNUPG),
                "--decrypt",
                "--output",
                str(age_ct),
                str(archive),
            ]
        )
        if r.returncode != 0:
            print(
                json.dumps(
                    {
                        "command": "remote-verify",
                        "archive": archive.name,
                        "status": "error",
                        "error": "gpg decrypt failed",
                    }
                )
            )
            return 2
        plain = tmp / "artifact.tar.gz"
        r = run([str(AGE_BIN), "-d", "-i", str(AGE_KEY), "-o", str(plain), str(age_ct)])
        if r.returncode != 0:
            print(
                json.dumps(
                    {
                        "command": "remote-verify",
                        "archive": archive.name,
                        "status": "error",
                        "error": "age decrypt failed",
                    }
                )
            )
            return 2
        extract_dir = tmp / "x"
        extract_dir.mkdir()
        try:
            with tarfile.open(plain) as tf:
                tf.extractall(extract_dir, filter="data")
        except (tarfile.TarError, OSError) as e:
            print(
                json.dumps(
                    {
                        "command": "remote-verify",
                        "archive": archive.name,
                        "status": "error",
                        "error": f"tar extract failed: {e}",
                    }
                )
            )
            return 2
        report, code = verify_extracted(extract_dir)
        print(json.dumps({"command": "remote-verify", "archive": archive.name, **report}))
        return code
    finally:
        # [WHY] decrypted plaintext must never persist on the backup server.
        shutil.rmtree(tmp, ignore_errors=True)


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
