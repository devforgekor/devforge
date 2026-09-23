#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/test_kv_safe.py — scripts/deploy/kv-safe.py 비교 정규화
"""kv-safe.py `compare`의 공백 무시 비교 회귀 테스트.

KV는 멀티라인(PEM) 저장/조회 시 개행을 공백으로 치환하므로, raw 비교는
개행↔공백 차이만으로 false MISMATCH를 낸다(2026-09-23 확인: OCI RSA 키 SP=32).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

KV_SAFE = Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "kv-safe.py"

PEM_LOCAL = "-----BEGIN KEY-----\nAAAA\nBBBB\n-----END KEY-----\n"
PEM_KV = "-----BEGIN KEY----- AAAA BBBB -----END KEY-----"


@pytest.fixture(scope="module")
def kv_safe():
    spec = importlib.util.spec_from_file_location("kv_safe", KV_SAFE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_should_match_when_only_newlines_became_spaces(kv_safe):
    assert kv_safe._canonical(PEM_LOCAL) == kv_safe._canonical(PEM_KV)


def test_should_not_match_when_payload_differs(kv_safe):
    assert kv_safe._canonical(PEM_LOCAL) != kv_safe._canonical(PEM_KV.replace("BBBB", "CCCC"))


def test_compare_should_report_match_for_pem_roundtrip(kv_safe, tmp_path, monkeypatch, capsys):
    local = tmp_path / "key.pem"
    local.write_text(PEM_LOCAL)
    monkeypatch.setattr(kv_safe, "_get_value", lambda vault, secret: PEM_KV)

    rc = kv_safe.cmd_compare("vault", "SECRET", str(local))

    assert rc == 0
    assert "MATCH" in capsys.readouterr().out


def test_compare_should_report_mismatch_when_value_differs(kv_safe, tmp_path, monkeypatch):
    local = tmp_path / "key.pem"
    local.write_text(PEM_LOCAL)
    monkeypatch.setattr(
        kv_safe, "_get_value", lambda vault, secret: "-----BEGIN KEY----- ZZZZ -----END KEY-----"
    )

    assert kv_safe.cmd_compare("vault", "SECRET", str(local)) == 3
