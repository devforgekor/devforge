#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/fitness/
"""Fitness: heartbeat registry SSOT stays bound to code, config, and units.

`docs/specs/heartbeat-registry.yaml` is the F2 SSOT. A silent drift between the
registry, `heartbeat_workers` (v2 config), and the unit `ExecStartPost` hooks
would make the dead-man's switch lie. This ties all three together in CI.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from devforge.core.config import WatchdogConfig

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "docs" / "specs" / "heartbeat-registry.yaml"
UNITS = ROOT / "systemd" / "user"


def _jobs() -> dict[str, dict]:
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))
    return data["jobs"]


def test_registry_max_age_matches_v2_config() -> None:
    workers = WatchdogConfig().heartbeat_workers
    missing = [job for job in _jobs() if job not in workers]
    assert not missing, f"registry jobs absent from heartbeat_workers: {missing}"
    for job, spec in _jobs().items():
        assert spec["max_age_sec"] == workers[job], f"{job}: registry vs config mismatch"


def test_registry_units_wire_the_ping() -> None:
    for job, spec in _jobs().items():
        unit = UNITS / Path(spec["unit"]).name
        assert unit.exists(), f"{job}: unit {spec['unit']} missing"
        text = unit.read_text(encoding="utf-8")
        assert f"heartbeat-ping.sh {job}" in text, f"{job}: no ping wiring in {unit.name}"
