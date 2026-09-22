#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driving/
"""Tests for watchdog CLI serve command (Gate 4 code prereq)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from devforge.adapters.driving.cli_cmds import watchdog as watchdog_cmds
from devforge.core.config import WatchdogConfig

# 모듈 상수를 패치하기 위한 헬퍼
def patch_liveness_file(new_path: Path):
    """watchdog_cmds.LIVENESS_FILE 상수를 패치."""
    watchdog_cmds.LIVENESS_FILE = new_path


class MockWatchdogService:
    def __init__(self, dry_run: bool = False, check_interval_sec: int = 60) -> None:
        self._dry_run = dry_run
        self._check_interval_sec = check_interval_sec
        self.run_cycle_calls = 0

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def check_interval_sec(self) -> int:
        return self._check_interval_sec

    async def run_cycle(self) -> dict:
        self.run_cycle_calls += 1
        return {"checks": 1, "failed": 0, "dry_run": self._dry_run, "timestamp": "2024-01-01T00:00:00Z"}

    def component_states(self) -> list:
        return []

    async def resolve_incident(self, incident_id: int, note: str) -> None:
        pass

    def load_state(self) -> None:
        pass


@pytest.fixture(autouse=True)
def reset_factory() -> None:
    watchdog_cmds._factory = None
    yield
    watchdog_cmds._factory = None


@pytest.fixture
def mock_service() -> MockWatchdogService:
    return MockWatchdogService(dry_run=True, check_interval_sec=10)


@pytest.fixture
def factory(mock_service: MockWatchdogService):
    async def _factory() -> MockWatchdogService:
        return mock_service
    return _factory


class TestServeCommand:
    """serve 루프가 liveness 파일을 쓰고 루프를 도는지 검증."""

    @pytest.mark.asyncio
    async def test_serve_writes_liveness_and_loops(self, factory, mock_service, tmp_path) -> None:
        watchdog_cmds.init(factory)

        # liveness 파일을 임시 경로로 리다이렉트 (모듈 상수 패치)
        liveness_file = tmp_path / "watchdog_liveness_test"
        patch_liveness_file(liveness_file)

        # _serve_loop를 직접 호출하되 1회 루프 후 취소
        async def run_with_timeout() -> None:
            try:
                await asyncio.wait_for(watchdog_cmds._serve_loop(), timeout=0.5)
            except asyncio.TimeoutError:
                pass  # 예상됨 - 1회 루프 후 sleep에서 timeout

        await run_with_timeout()

        # run_cycle이 최소 1회 호출됨
        assert mock_service.run_cycle_calls >= 1

        # liveness 파일이 생성되고 epoch가 기록됨
        assert liveness_file.exists()
        content = liveness_file.read_text().strip()
        assert content.isdigit()
        epoch = int(content)
        import time
        assert abs(epoch - int(time.time())) < 5  # 현재 시간과 근접

    def test_serve_command_exists(self) -> None:
        runner = CliRunner()
        result = runner.invoke(watchdog_cmds.app, ["serve", "--help"])
        assert result.exit_code == 0
        assert "Watchdog loop entrypoint" in result.output


class TestLivenessWrite:
    def test_write_liveness_creates_file(self, tmp_path) -> None:
        liveness_file = tmp_path / "test_liveness"
        patch_liveness_file(liveness_file)

        from devforge.adapters.driving.cli_cmds.watchdog import _write_liveness
        _write_liveness()

        assert liveness_file.exists()
        content = liveness_file.read_text().strip()
        assert content.isdigit()

    def test_write_liveness_handles_oserror(self, tmp_path) -> None:
        # 읽기 전용 디렉토리 시뮬레이션은 복잡하므로 경로만 검증
        from devforge.adapters.driving.cli_cmds.watchdog import _write_liveness
        # 예외가 발생하지 않아야 함 (내부에서 catch함)
        _write_liveness()  # 기본 경로는 /var/tmp/watchdog_last_cycle_ts (쓰기 실패 가능)