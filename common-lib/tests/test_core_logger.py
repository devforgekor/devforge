"""Tests for src/common_lib/core/logger.py"""
import logging
import os
from pathlib import Path

import pytest

from common_lib.core.logger import build_rotating_logger


class TestBuildRotatingLogger:
    def test_creates_logger(self, tmp_path):
        logger = build_rotating_logger("test.logger1", tmp_path / "app.log")
        assert isinstance(logger, logging.Logger)

    def test_creates_log_file(self, tmp_path):
        log_file = tmp_path / "logs" / "app.log"
        logger = build_rotating_logger("test.logger2", log_file)
        logger.info("test message")
        assert log_file.exists()
        assert "test message" in log_file.read_text(encoding="utf-8")

    def test_creates_parent_dirs(self, tmp_path):
        log_file = tmp_path / "deep" / "nested" / "app.log"
        build_rotating_logger("test.logger3", log_file)
        logger = logging.getLogger("test.logger3")
        logger.info("hello")
        assert log_file.exists()

    def test_idempotent_no_duplicate_handlers(self, tmp_path):
        log_file = tmp_path / "app.log"
        logger1 = build_rotating_logger("test.idem", log_file)
        handler_count = len(logger1.handlers)
        logger2 = build_rotating_logger("test.idem", log_file)
        assert len(logger2.handlers) == handler_count

    def test_log_level_respected(self, tmp_path):
        log_file = tmp_path / "debug.log"
        logger = build_rotating_logger("test.level", log_file, level=logging.DEBUG)
        assert logger.level == logging.DEBUG

    def test_console_handler_added_when_env_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_CONSOLE", "true")
        log_file = tmp_path / "console.log"
        logger = build_rotating_logger("test.console", log_file)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" in handler_types

    def test_no_console_handler_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOG_CONSOLE", raising=False)
        log_file = tmp_path / "noconsole.log"
        logger = build_rotating_logger("test.noconsole", log_file)
        handler_types = [type(h).__name__ for h in logger.handlers]
        assert "StreamHandler" not in handler_types
