#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driving/cli_cmds/ (composition root: cli.py)
"""Watchdog CLI commands (driving adapter — factory injected, no application import)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import typer

app = typer.Typer(name="watchdog", help="Watchdog operations")
_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None


def init(factory: Callable[[], Coroutine[Any, Any, Any]]) -> None:
    global _factory
    _factory = factory


def _build_service() -> Any:
    if _factory is None:
        raise RuntimeError("watchdog.init() not called from composition root")
    return asyncio.run(_factory())


@app.command("status")
def status() -> None:
    """Print per-component tracker state."""
    svc = _build_service()
    for s in svc.component_states():
        typer.echo(f"{s['name']}: {s['state']} fails={s['fail_count']} circuit={s['circuit_open']}")


@app.command("check")
def check() -> None:
    """Run one health-check cycle."""
    result = asyncio.run(_build_service().run_cycle())
    typer.echo(f"checks={result['checks']} failed={result['failed']}")


@app.command("resolve")
def resolve(incident_id: int, note: str) -> None:
    """Resolve an incident by id."""
    svc = _build_service()
    asyncio.run(svc.resolve_incident(incident_id, note))
    typer.echo("resolved=True")
