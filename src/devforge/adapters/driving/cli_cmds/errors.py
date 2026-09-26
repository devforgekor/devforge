#!/usr/bin/env python3
# Status: experimental
# Path: cli.py (composition root: cli.py → errors_cmds.init)
"""Error-record analysis CLI (§2) — read-only decision packet."""

from __future__ import annotations

import asyncio
import json as json_module
from datetime import datetime, timedelta, timezone
from typing import Callable

import typer

app = typer.Typer(name="errors", help="Error-record analysis (§2, read-only)")
_factory: Callable[[], object] | None = None


def init(factory: Callable[[], object]) -> None:
    global _factory
    _factory = factory


@app.command("analyze")
def analyze(
    since: str = typer.Option(None, "--since", help="ISO start (default: --days ago)"),
    days: int = typer.Option(7, "--days", "-d", help="Look back N days when --since omitted"),
    llm: bool = typer.Option(
        False, "--llm/--no-llm", help="Add LLM root-cause hypotheses (needs OPENROUTER keys)"
    ),
) -> None:
    """Analyze structured incidents into a decision packet (no execution)."""
    if _factory is None:
        raise RuntimeError("errors.init() not called from composition root")
    since_iso = since or (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    service = _factory()
    packet = asyncio.run(service.analyze(since_iso, use_llm=llm))  # type: ignore[attr-defined]
    typer.echo(json_module.dumps(packet, indent=2, ensure_ascii=False, default=str))
