#!/usr/bin/env python3.12
# Status: experimental
# Path: tests/unit/test_heartbeat_pulses.py — scripts/pipelines/embed_batch.py
"""embed_batch 종료 시 heartbeat pulse 정리 회귀 테스트.

[WHY] liveness 스레드는 프로세스 종료와 함께 죽어 pulse 가 IN_PROGRESS 로 남고,
watchdog(HeartbeatHealthChecker)은 max_age 1800s 초과를 DOWN 으로 본다.
2026-09-29 확인: heartbeat_liveness_embed_batch 2695회 연속 실패 → 300초마다
알림. Slack 토큰(account_inactive)까지 죽어 알림이 통째로 묻혔다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from pipelines import embed_batch  # noqa: E402


def test_cleanup_pulses_should_resolve_liveness_then_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def _record(pulse_id: str, *args, **kwargs) -> bool:
        calls.append(pulse_id)
        return True

    monkeypatch.setattr(embed_batch, "resolve_pulse", _record)

    embed_batch._cleanup_pulses()

    assert calls == ["heartbeat_liveness_embed_batch", "heartbeat_embed_batch"]


def test_cleanup_pulses_is_registered_as_process_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """종료 훅이 실제 liveness pulse 를 놓치지 않는지 상수로 고정한다."""
    source = Path(embed_batch.__file__).read_text(encoding="utf-8")

    assert "_cleanup_embed = _cleanup_pulses" in source
    assert 'resolve_pulse("heartbeat_liveness_embed_batch")' in source
    assert os.path.exists(embed_batch.__file__)
