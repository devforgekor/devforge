#!/usr/bin/env python3
# Status: production
# Path: baseline-daily.py
"""Hook overhead measurement — safe run (no DB writes, mocked observe).

W1 baseline spec: docs/ops/baseline/W1-baseline-spec.md
Run: python3 scripts/hooks/measure-hook-overhead.py [--live]
  --live  : measure with real observe() (writes to DB, optional)
Without --live: mock observe() for safe baseline overhead estimate.
"""
import json
import os
import sys
import time
import unittest.mock as mock

_HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_DIR = os.path.dirname(_HOOKS_DIR)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# Mock observe() before importing auto_log to avoid DB writes during safe mode
if "--live" not in sys.argv:
    mock_observe = mock.MagicMock(return_value="mocked-obs-id")
    sys.modules["lib.db"] = mock.MagicMock()
    sys.modules["lib.db"].esc_sql = lambda s: s
    sys.modules["lib.db"].psql = lambda *a, **k: ""
    sys.modules["lib.db"].psql_json = lambda *a, **k: {}

from lib.observation import observe as _real_observe
import auto_log


def measure(iterations: int = 100) -> dict:
    # tool_input must contain a real command so _handle_bash does its work
    # (pretool UPDATE + observe for test commands). Using a test command
    # exercises the full path including the observe() DB write.
    test_input = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest scripts/tests/"},
        "tool_output": {"exitCode": 0},
    }
    times = []
    for _ in range(iterations):
        start = time.perf_counter()
        try:
            auto_log._handle_bash(test_input["tool_input"], test_input["tool_output"])
        except Exception:
            pass
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
    sorted_times = sorted(times)
    return {
        "iterations": iterations,
        "avg_ms": round(sum(times) / len(times), 4),
        "p50_ms": round(sorted_times[len(sorted_times) // 2], 4),
        "p95_ms": round(sorted_times[int(len(sorted_times) * 0.95)], 4),
        "p99_ms": round(sorted_times[int(len(sorted_times) * 0.99)], 4),
        "min_ms": round(min(times), 4),
        "max_ms": round(max(times), 4),
        "mode": "safe(mock)" if "--live" not in sys.argv else "live",
    }


if __name__ == "__main__":
    result = measure()
    print(json.dumps(result, indent=2))
    outdir = "/opt/projects/server/docs/ops/baseline"
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "hook-overhead-initial.json"), "w") as f:
        json.dump(result, f, indent=2)
