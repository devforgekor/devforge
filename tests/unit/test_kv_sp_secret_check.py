#!/usr/bin/env python3
# Status: production
# Path: tests/unit/test_kv_sp_secret_check.py — scripts/deploy/kv-sp-secret-check.py
"""SP secret expiry classification and metadata parsing."""

import datetime
import importlib.util
import json
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "deploy" / "kv-sp-secret-check.py"
)
_spec = importlib.util.spec_from_file_location("kv_sp_secret_check", SCRIPT)
assert _spec and _spec.loader
kv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kv)

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def test_should_alert_when_credential_invalid():
    ok, msg = kv.classify(NOW + datetime.timedelta(days=365), NOW, invalid=True)
    assert ok is False
    assert "invalid" in msg


def test_should_alert_when_expiring_within_critical():
    ok, msg = kv.classify(NOW + datetime.timedelta(days=3), NOW, invalid=False)
    assert ok is False
    assert "critical" in msg


def test_should_alert_when_expiring_within_warn():
    ok, msg = kv.classify(NOW + datetime.timedelta(days=20), NOW, invalid=False)
    assert ok is False
    assert "warn" in msg


def test_should_pass_when_far_future():
    ok, msg = kv.classify(NOW + datetime.timedelta(days=365), NOW, invalid=False)
    assert ok is True
    assert "valid" in msg


def test_should_pass_with_warning_when_expiry_unknown():
    ok, msg = kv.classify(None, NOW, invalid=False)
    assert ok is True
    assert "expiry unknown" in msg


def test_parse_expiry_should_handle_date_and_z_suffix():
    assert kv.parse_expiry("2027-09-20").year == 2027
    parsed = kv.parse_expiry("2027-09-20T00:00:00Z")
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_expiry_should_return_none_for_invalid():
    assert kv.parse_expiry("not-a-date") is None
    assert kv.parse_expiry(None) is None


def test_load_expiry_should_read_metadata(tmp_path):
    meta = tmp_path / "meta.json"
    meta.write_text(json.dumps({"expires_at": "2028-01-01"}))
    parsed = kv.load_expiry(str(meta))
    assert parsed is not None and parsed.year == 2028


def test_load_expiry_should_return_none_when_missing(tmp_path):
    assert kv.load_expiry(str(tmp_path / "absent.json")) is None
