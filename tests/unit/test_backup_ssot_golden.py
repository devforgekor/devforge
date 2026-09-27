#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/
"""Unit tests for backup_ssot_golden — manifest integrity and remote command flow.

All crypto/ssh calls are mocked: devforge must never run gpg/age (architecture A —
encryption and verification live only on the onmydoc backup server).
"""

from __future__ import annotations

import argparse
import hashlib
import io
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


def _out_path(cmd: list[str]) -> Path | None:
    for flag in ("-o", "--output"):
        if flag in cmd:
            return Path(cmd[cmd.index(flag) + 1])
    return None


def _make_src_tree(root: Path) -> Path:
    src = root / "src"
    src.mkdir()
    (src / "doc.md").write_text("hello ssot", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "spec.yaml").write_text("k: v", encoding="utf-8")
    return src


def _build_artifact(root: Path, *, sha_override: str | None = None) -> Path:
    work = root / "work"
    work.mkdir()
    (work / "a.txt").write_text("v1", encoding="utf-8")
    sha = sha_override or hashlib.sha256(b"v1").hexdigest()
    manifest = {
        "schema": 1,
        "git_commit": "test",
        "files": [{"path": "a.txt", "sha256": sha, "size": 2}],
    }
    tar_path = root / "art.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(work / "a.txt", arcname="a.txt")
        blob = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        tf.addfile(info, io.BytesIO(blob))
    return tar_path


def _prepare_verify(tmp_path: Path, monkeypatch, payload: bytes) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "a.tar.gz.age.gpg").write_bytes(payload)
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    monkeypatch.setattr(bs, "GNUPG", tmp_path / "gnupg")
    monkeypatch.setattr(bs, "AGE_KEY", tmp_path / "age-key.txt")
    monkeypatch.setattr(bs, "AGE_BIN", tmp_path / "bin" / "age")


def _fake_decrypt_chain(payload: bytes):
    def fake_run(cmd: list[str]) -> FakeRC:
        if "--decrypt" in cmd:
            _out_path(cmd).write_bytes(payload)  # type: ignore[union-attr]
            return FakeRC()
        if "-d" in cmd:
            out = _out_path(cmd)
            assert out is not None
            out.write_bytes(Path(cmd[-1]).read_bytes())  # type: ignore[union-attr]
            return FakeRC()
        return FakeRC()

    return fake_run


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


def test_should_encrypt_with_age_then_gpg_when_receive(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    archive = tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    (inbox / "ssot-golden-x.tar.gz").write_bytes(b"plain-tar")
    age_key = tmp_path / "age-key.txt"
    age_key.write_text("# public key: age1test\nAGESECRET\n", encoding="utf-8")
    monkeypatch.setattr(bs, "INBOX", inbox)
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "AGE_KEY", age_key)
    monkeypatch.setattr(bs, "GNUPG", tmp_path / "gnupg")
    monkeypatch.setattr(bs, "AGE_BIN", tmp_path / "bin" / "age")
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> FakeRC:
        calls.append(list(cmd))
        if "--list-secret-keys" in cmd:
            return FakeRC(stdout="sec:-:255:1::\nfpr:::::::::FPR123:\n")
        out = _out_path(cmd)
        if out is not None:
            out.write_bytes(b"cipher")
        return FakeRC()

    monkeypatch.setattr(bs, "run", fake_run)
    rc = bs.remote_receive(argparse.Namespace(file="ssot-golden-x.tar.gz"))
    assert rc == 0
    age_idx = next(i for i, c in enumerate(calls) if str(c[0]).endswith("/age"))
    gpg_idx = next(i for i, c in enumerate(calls) if c[0] == "gpg" and "--encrypt" in c)
    assert age_idx < gpg_idx
    assert list(archive.glob("*.age.gpg"))
    assert not list(inbox.iterdir())


def test_should_report_missing_changed_extra_when_manifest_compared(
    tmp_path: Path,
) -> None:
    (tmp_path / "good.txt").write_text("ok", encoding="utf-8")
    (tmp_path / "bad.txt").write_text("new", encoding="utf-8")
    (tmp_path / "rogue.txt").write_text("x", encoding="utf-8")
    manifest = {
        "files": [
            {"path": "good.txt", "sha256": hashlib.sha256(b"ok").hexdigest()},
            {"path": "bad.txt", "sha256": hashlib.sha256(b"old").hexdigest()},
            {"path": "gone.txt", "sha256": hashlib.sha256(b"gone").hexdigest()},
        ]
    }
    report = bs.compare_manifest(tmp_path, manifest)
    assert report["status"] == "fail"
    assert report["missing"] == ["gone.txt"]
    assert report["changed"] == ["bad.txt"]
    assert report["extra"] == ["rogue.txt"]


def test_should_pass_when_archive_intact(tmp_path: Path, monkeypatch) -> None:
    tar_path = _build_artifact(tmp_path)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "a.tar.gz.age.gpg").write_bytes(tar_path.read_bytes())
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    monkeypatch.setattr(bs, "GNUPG", tmp_path / "gnupg")
    monkeypatch.setattr(bs, "AGE_KEY", tmp_path / "age-key.txt")
    monkeypatch.setattr(bs, "AGE_BIN", tmp_path / "bin" / "age")

    def fake_run(cmd: list[str]) -> FakeRC:
        if "--decrypt" in cmd:
            _out_path(cmd).write_bytes(tar_path.read_bytes())  # type: ignore[union-attr]
            return FakeRC()
        if "-d" in cmd:
            out = _out_path(cmd)
            assert out is not None
            out.write_bytes(Path(cmd[-1]).read_bytes())  # type: ignore[union-attr]
            return FakeRC()
        return FakeRC()

    monkeypatch.setattr(bs, "run", fake_run)
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 0


def test_should_fail_when_manifest_sha_mismatch(tmp_path: Path, monkeypatch) -> None:
    tar_path = _build_artifact(tmp_path, sha_override="0" * 64)
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "a.tar.gz.age.gpg").write_bytes(tar_path.read_bytes())
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    monkeypatch.setattr(bs, "GNUPG", tmp_path / "gnupg")
    monkeypatch.setattr(bs, "AGE_KEY", tmp_path / "age-key.txt")
    monkeypatch.setattr(bs, "AGE_BIN", tmp_path / "bin" / "age")

    def fake_run(cmd: list[str]) -> FakeRC:
        if "--decrypt" in cmd:
            _out_path(cmd).write_bytes(tar_path.read_bytes())  # type: ignore[union-attr]
            return FakeRC()
        if "-d" in cmd:
            out = _out_path(cmd)
            assert out is not None
            out.write_bytes(Path(cmd[-1]).read_bytes())  # type: ignore[union-attr]
            return FakeRC()
        return FakeRC()

    monkeypatch.setattr(bs, "run", fake_run)
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 1


def test_should_fail_with_exit_2_when_gpg_decrypt_fails(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "a.tar.gz.age.gpg").write_bytes(b"broken")
    monkeypatch.setattr(bs, "ARCHIVE", archive)
    monkeypatch.setattr(bs, "RDIR", tmp_path / "remote")
    monkeypatch.setattr(bs, "GNUPG", tmp_path / "gnupg")
    monkeypatch.setattr(bs, "AGE_KEY", tmp_path / "age-key.txt")

    def fake_run(cmd: list[str]) -> FakeRC:
        if "--decrypt" in cmd:
            return FakeRC(returncode=2, stderr="gpg: decryption failed")
        return FakeRC()

    monkeypatch.setattr(bs, "run", fake_run)
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"] == "gpg decrypt failed"


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


def test_should_fail_with_exit_2_when_tar_corrupt_after_decrypt(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _prepare_verify(tmp_path, monkeypatch, b"cipher")
    monkeypatch.setattr(bs, "run", _fake_decrypt_chain(b"cipher"))
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"].startswith("tar extract failed")


def test_should_fail_with_exit_2_when_manifest_malformed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("v1", encoding="utf-8")
    tar_path = tmp_path / "art.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(work / "a.txt", arcname="a.txt")
        blob = json.dumps({"files": "not-a-list"}).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(blob)
        tf.addfile(info, io.BytesIO(blob))
    _prepare_verify(tmp_path, monkeypatch, tar_path.read_bytes())
    monkeypatch.setattr(bs, "run", _fake_decrypt_chain(tar_path.read_bytes()))
    rc = bs.remote_verify(argparse.Namespace(file=None))
    assert rc == 2
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["error"] == "manifest malformed"


def test_should_raise_backuperror_when_binary_missing() -> None:
    with pytest.raises(bs.BackupError, match="cannot execute"):
        bs.run(["/nonexistent/devforge-missing-bin-9x", "--version"])


def test_should_print_single_json_line_when_ship_succeeds(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    src = _make_src_tree(tmp_path)
    monkeypatch.setattr(bs, "SSOT_SOURCES", [(src, ["doc.md"])])
    monkeypatch.setattr(bs, "STAGE", tmp_path / "stage")

    def fake_run(cmd: list[str]) -> FakeRC:
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
    assert payload["remote"]["status"] == "ok"
    assert "remote trace" in captured.err
