"""Characterization: watchdog liveness heartbeat (Week 2, test 4).

Captures the dead-man's-switch contract (lib/watchdog):
  - the liveness file path is a fixed, well-known location
  - every loop writes the current epoch seconds to it
  - a stale value (older than the limit) is what the liveness timer detects

No live loop: the write function is called directly against a temp path.
"""

import time

import pytest

pytestmark = pytest.mark.characterization


def test_liveness_file_path_constant():
    from lib.watchdog import config

    assert config.WATCHDOG_LIVENESS_FILE == "/var/tmp/watchdog_last_cycle_ts"


def test_write_liveness_writes_current_timestamp(tmp_path, monkeypatch):
    orch = pytest.importorskip("lib.watchdog.orchestrator")
    target = tmp_path / "liveness"
    monkeypatch.setattr(orch, "WATCHDOG_LIVENESS_FILE", str(target))

    before = time.time()
    orch._write_liveness()
    after = time.time()

    value = float(target.read_text().strip())
    assert before <= value <= after
