#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/storage/
"""Tests for JsonStateStorage (A5)."""
from __future__ import annotations

from devforge.adapters.driven.storage.state_json import JsonStateStorage
from devforge.domain.watchdog.monitoring.tracker import ComponentTracker


def test_roundtrip_legacy_shape(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = JsonStateStorage(path=str(tmp_path / "s.json"))
    t = ComponentTracker("svc:x")
    t.record_failure()
    t.record_failure()
    assert storage.save({"svc:x": t}, last_heartbeat_ts=123.0, mode="day")
    payload = storage.load()
    assert set(payload) == {"components", "last_heartbeat_ts", "mode"}
    assert payload["last_heartbeat_ts"] == 123.0 and payload["mode"] == "day"
    assert payload["components"][0]["consecutive_fail"] == 2


def test_atomic_no_tmp_left(tmp_path) -> None:  # type: ignore[no-untyped-def]
    storage = JsonStateStorage(path=str(tmp_path / "s.json"))
    storage.save({"svc:x": ComponentTracker("svc:x")}, 0.0, "day")
    assert list(tmp_path.glob(".watchdog_state_*.tmp")) == []


def test_missing_file_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    assert JsonStateStorage(path=str(tmp_path / "nope.json")).load() == {}


def test_corrupt_file_empty(tmp_path) -> None:  # type: ignore[no-untyped-def]
    p = tmp_path / "c.json"
    p.write_text("{bad")
    assert JsonStateStorage(path=str(p)).load() == {}


def test_legacy_file_is_readable(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Parity guard: a v1-format file must load (existing production file)."""
    p = tmp_path / "legacy.json"
    p.write_text('{"components": [{"name": "svc:x", "state": "UNHEALTHY", '
                 '"fail_count": 3, "consecutive_fail": 3}], '
                 '"last_heartbeat_ts": 9.0, "mode": "night"}')
    payload = JsonStateStorage(path=str(p)).load()
    assert payload["mode"] == "night" and payload["components"][0]["state"] == "UNHEALTHY"
