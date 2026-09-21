"""Characterization: watchdog check_all_llm T1/T2 probe (Week 2, test 2).

Captures the CURRENT probing contract (lib/watchdog/checker.py):
  - only the port currently serving a model is probed (others skipped)
  - T2 (deep probe) runs ONLY if T1 (health) succeeded
  - each result dict has keys: name, port, t1_ok, t1_detail, t2_ok, t2_detail

No live ports: health/probe functions and mode are monkeypatched.
"""

import pytest

checker = pytest.importorskip("lib.watchdog.checker")

pytestmark = pytest.mark.characterization

PORT = 8082


def _patch(monkeypatch, *, inference_port=PORT, health=(True, "health-ok"),
           probe=(True, "probe-ok")):
    monkeypatch.setattr(checker, "LLM_TARGETS", {"t": {"port": PORT, "label": "T"}})
    monkeypatch.setattr(checker, "DAY_PORTS", {PORT})
    monkeypatch.setattr(checker, "read_mode", lambda: "day")
    monkeypatch.setattr(checker, "_current_inference_port", lambda: inference_port)
    monkeypatch.setattr(checker, "check_health", lambda p, label: health)
    monkeypatch.setattr(checker, "check_llm_probe", lambda p, label: probe)


def test_result_shape_and_t1_t2(monkeypatch):
    _patch(monkeypatch)
    results = checker.check_all_llm()
    assert len(results) == 1
    assert results[0] == {
        "name": "t",
        "port": PORT,
        "t1_ok": True,
        "t1_detail": "health-ok",
        "t2_ok": True,
        "t2_detail": "probe-ok",
    }


def test_t2_not_run_when_t1_fails(monkeypatch):
    _patch(monkeypatch, health=(False, "down"))
    calls = []
    monkeypatch.setattr(checker, "check_llm_probe", lambda p, label: calls.append(p) or (True, "x"))
    results = checker.check_all_llm()
    assert results[0]["t1_ok"] is False
    assert results[0]["t2_ok"] is False
    assert results[0]["t2_detail"] == ""
    assert calls == []


def test_inactive_port_is_skipped(monkeypatch):
    _patch(monkeypatch, inference_port=9999)
    assert checker.check_all_llm() == []
