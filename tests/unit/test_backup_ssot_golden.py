#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for backup_ssot_golden — ciphertext ship/receive flow and integrity.

Crypto is mocked: devforge encrypts locally with the shared age recipient
(architecture B, unified with kv-backup.py); the onmydoc relay stores ciphertext
only and verifies sha256 sidecars. Restore rehearsal runs on the identity PC.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
from pathlib import Path

import backup_ssot_golden as bs
import pytest


class FakeRC:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_src_tree(root: Path) -> Path:
    src = root / "src"
    src.mkdir()
    (src / "doc.md").write_text("hello ssot", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "spec.yaml").write_text("k: v", encoding="utf-8")
    return src


def _write_cipher_archive(archive: Path, payload: bytes) -> str:
    """ciphertext + sidecar를 archive에 배치하고 파일명을 반환."""
    name = "a.tar.gz.age"
    (archive / name).write_bytes(payload)
    (archive / f"{name}.sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n", encoding="utf-8"
    )
    return name


def test_should_collect_files_when_patterns_match(tmp_path: Path, monkeypatch) -> None:
    src = _make_src_tree(tmp_path)
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md", "sub/*"])])
    files, unmatched = bs.collect()
    assert unmatched == []
    assert len(files) == 2
    assert {Path(p).name for p in files} == {"doc.md", "spec.yaml"}


def test_should_raise_when_exact_pattern_missing(tmp_path: Path, monkeypatch) -> None:
    src = _make_src_tree(tmp_path)
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md", "gone.yaml"])])
    try:
        bs.collect()
        raise AssertionError("expected BackupError")
    except bs.BackupError as e:
        assert "gone.yaml" in str(e)


def test_should_record_unmatched_when_glob_matches_nothing(tmp_path: Path, monkeypatch) -> None:
    src = _make_src_tree(tmp_path)
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md", "empty/*"])])
    files, unmatched = bs.collect()
    assert len(files) == 1
    assert unmatched and unmatched[0].endswith("empty/*")


def test_should_contain_matching_sha256_when_manifest_built(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("hello", encoding="utf-8")
    manifest = bs.build_manifest({"tmp/x/f.txt": f}, [], "deadbeef")
    entry = manifest["files"][0]
    assert entry["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert entry["path"] == "tmp/x/f.txt"
    assert manifest["git_commit"] == "deadbeef"


def test_should_pack_manifest_and_files_when_stage_runs(tmp_path: Path, monkeypatch) -> None:
    src = _make_src_tree(tmp_path)
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md", "sub/*"])])
    monkeypatch.setattr(bs, "STAGE", tmp_path / "stage")
    tar_path, manifest = bs.stage(commit="deadbeef")
    assert tar_path.is_file()
    assert len(manifest["files"]) == 2
    with tarfile.open(tar_path) as tf:
        names = tf.getnames()
        assert bs.MANIFEST_NAME in names
        assert any(n.endswith("doc.md") for n in names)
        packed = json.load(tf.extractfile(bs.MANIFEST_NAME))  # type: ignore[arg-type]
    assert packed["git_commit"] == "deadbeef"
    assert len(packed["files"]) == 2


def test_should_skip_ssh_and_scp_when_ship_dry_run(tmp_path: Path, monkeypatch, capsys) -> None:
    src = _make_src_tree(tmp_path)
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md"])])
    monkeypatch.setattr(bs, "STAGE", tmp_path / "stage")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> FakeRC:
        calls.append(list(cmd))
        return FakeRC()

    monkeypatch.setattr(bs, "run", fake_run)
    rc = bs.ship(argparse.Namespace(dry_run=True, oci=False))
    assert rc == 0
    joined = [" ".join(c) for c in calls]
    assert not any(c.startswith("ssh") or c.startswith("scp") for c in joined)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "dry-run"
    assert out["files"] == 1
    assert list((tmp_path / "stage").glob("*.tar.gz"))


def test_should_verify_sidecar_and_move_when_receive(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    payload = b"ciphertext-bytes"
    name = "ssot-golden-x.tar.gz.age"
    (inbox / name).write_bytes(payload)
    (inbox / f"{name}.sha256").write_text(
        f"{hashlib.sha256(payload).hexdigest()}  {name}\n", encoding="utf-8"
    )
    monkeypatch.setattr(bs, "INBOX", inbox)
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    rc = bs.remote_receive(argparse.Namespace(file=name))
    assert rc == 0
    assert (archive / name).is_file()
    assert (archive / f"{name}.sha256").is_file()
    assert not list(inbox.iterdir())


def test_should_raise_when_sidecar_missing_on_receive(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "x.tar.gz.age").write_bytes(b"cipher")
    monkeypatch.setattr(bs, "INBOX", inbox)
    monkeypatch.setattr(bs, "ARCHIVE", tmp_path / "archive")
    with pytest.raises(bs.BackupError, match="sidecar"):
        bs.remote_receive(argparse.Namespace(file="x.tar.gz.age"))


def test_should_raise_when_sidecar_sha_mismatch_on_receive(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "x.tar.gz.age").write_bytes(b"cipher")
    (inbox / "x.tar.gz.age.sha256").write_text(f"{'0' * 64}  x.tar.gz.age\n", encoding="utf-8")
    monkeypatch.setattr(bs, "INBOX", inbox)
    monkeypatch.setattr(bs, "ARCHIVE", tmp_path / "archive")
    with pytest.raises(bs.BackupError, match="sha256 mismatch"):
        bs.remote_receive(argparse.Namespace(file="x.tar.gz.age"))


def test_should_fail_with_exit_2_when_not_age_file(tmp_path: Path, monkeypatch, capsys) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    name = _write_cipher_archive(archive, b"broken")
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"] == "not an age ciphertext file"
    assert out["archive"] == name


def test_should_pass_when_archive_intact(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _write_cipher_archive(archive, b"age-encryption.org/v1\n-> stanza")
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 0


def test_should_fail_when_sidecar_sha_mismatch(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    name = _write_cipher_archive(archive, b"age-encryption.org/v1\n-> stanza")
    (archive / f"{name}.sha256").write_text(f"{'0' * 64}  {name}\n", encoding="utf-8")
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 1


def test_should_fail_with_exit_2_when_sidecar_missing(tmp_path: Path, monkeypatch, capsys) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "a.tar.gz.age").write_bytes(b"age-encryption.org/v1\n-> x")
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"] == "sidecar missing"


def test_should_reject_traversal_when_verify_file_arg_unsafe(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    with pytest.raises(bs.BackupError, match="unsafe"):
        bs.remote_verify(argparse.Namespace(file="../evil.tar.gz"))
    with pytest.raises(bs.BackupError, match="unsafe"):
        bs.remote_verify(argparse.Namespace(file="sub/evil.tar.gz"))


def test_should_accept_basename_only_when_validating_names() -> None:
    assert bs._safe_name("ssot-golden-x.tar.gz.age.gpg") == "ssot-golden-x.tar.gz.age.gpg"
    for bad in ("", ".", "..", "../x", "a/b", "/etc/passwd"):
        with pytest.raises(bs.BackupError):
            bs._safe_name(bad)


def test_should_fail_with_exit_2_when_sidecar_unreadable(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    _write_cipher_archive(archive, b"age-encryption.org/v1\n-> x")
    sidecar = Path(str(archive / "a.tar.gz.age") + ".sha256")
    sidecar.unlink()
    sidecar.mkdir()  # 디렉토리로 만들어 read_text 실패 유도
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"].startswith("sidecar unreadable")


def test_should_raise_backuperror_when_binary_missing() -> None:
    with pytest.raises(bs.BackupError, match="cannot execute"):
        bs.run(["/nonexistent/devforge-missing-bin-9x", "--version"])


def test_should_print_single_json_line_when_ship_succeeds(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    src = _make_src_tree(tmp_path)
    recipient = tmp_path / "recipient"
    recipient.write_text("age1test\n", encoding="utf-8")
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md"])])
    monkeypatch.setattr(bs, "STAGE", tmp_path / "stage")
    monkeypatch.setattr(bs, "AGE_RECIPIENT_FILE", recipient)

    def fake_run(cmd: list[str]) -> FakeRC:
        if str(cmd[0]).endswith("age") and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"age-ciphertext")
            return FakeRC()
        if cmd[0] == "ssh":
            return FakeRC(
                stdout='{"command":"remote-receive","status":"ok"}\n',
                stderr="remote trace\n",
            )
        return FakeRC()

    monkeypatch.setattr(bs, "run", fake_run)
    rc = bs.ship(argparse.Namespace(dry_run=False, oci=False))
    assert rc == 0
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.strip().splitlines() if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["command"] == "ship"
    assert payload["archive"].endswith(".tar.gz.age")
    assert payload["remote"]["status"] == "ok"
    assert "remote trace" in captured.err
