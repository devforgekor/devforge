#!/usr/bin/env python3
# Status: experimental
# Path: tests/unit/adapters/driven/storage/
"""Tests for structured incident context capture (error-record-design §1)."""
from __future__ import annotations

import pytest

from devforge.adapters.driven.storage import incident_pg


def test_mask_four_patterns() -> None:
    text = (
        "Authorization: Bearer abc.def.ghi\n"
        "OPENROUTER_API_KEY=sk-1234567890abcdef\n"
        "password=hunter2 token: xyz\n"
        "-----BEGIN PRIVATE KEY-----\nMIIsecret\n-----END PRIVATE KEY-----"
    )
    masked = incident_pg._mask(text)
    assert "abc.def.ghi" not in masked
    assert "sk-1234567890abcdef" not in masked
    assert "hunter2" not in masked
    assert "MIIsecret" not in masked
    assert "***PRIVATE KEY***" in masked


@pytest.mark.asyncio
async def test_capture_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(cmd: list[str], timeout: int) -> str | None:
        if cmd[0] == "systemctl":
            return "ActiveState=failed\nResult=exit-code\nExecMainStatus=15"
        if cmd[0] == "journalctl":
            return "line one\nline two token=secret"
        return None

    monkeypatch.setattr(incident_pg, "_run_capture", _fake_run)
    ctx = await incident_pg._capture_context_jsonb("svc:ebook-watcher", None)
    assert ctx["schema_version"] == 1
    assert ctx["unit"] == "ebook-watcher"
    assert ctx["systemd"]["ActiveState"] == "failed"
    assert ctx["systemd"]["ExecMainStatus"] == "15"
    assert ctx["journal_tail"] == ["line one", "line two token=***"]
    assert ctx["truncated"] is False


@pytest.mark.asyncio
async def test_capture_container_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_run(cmd: list[str], timeout: int) -> str | None:
        if cmd[0] == "podman":
            return "container log line"
        return None

    monkeypatch.setattr(incident_pg, "_run_capture", _fake_run)
    ctx = await incident_pg._capture_context_jsonb("svc:container-devforge-mcp", None)
    assert ctx["container"]["logs_tail"] == "container log line"


@pytest.mark.asyncio
async def test_capture_best_effort_no_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_tools(cmd: list[str], timeout: int) -> str | None:
        return None

    monkeypatch.setattr(incident_pg, "_run_capture", _no_tools)
    ctx = await incident_pg._capture_context_jsonb("svc:ebook-watcher", None)
    # missing tools (e.g. inside the v2 container) -> absent sections, not error
    assert "systemd" not in ctx
    assert "journal_tail" not in ctx
