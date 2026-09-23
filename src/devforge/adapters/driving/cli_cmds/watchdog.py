#!/usr/bin/env python3
# Status: experimental
# Path: adapters/driving/cli_cmds/ (composition root: cli.py)
"""Watchdog CLI commands (driving adapter — factory injected, no application import)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import typer

app = typer.Typer(name="watchdog", help="Watchdog operations")
_factory: Callable[[], Coroutine[Any, Any, Any]] | None = None
log = logging.getLogger(__name__)

LIVENESS_FILE = Path(os.environ.get("WATCHDOG_LIVENESS_FILE", "/var/tmp/watchdog_last_cycle_ts"))


def _write_liveness() -> None:
    """[WHY] legacy orchestrator._write_liveness parity — keeps devforge-watchdog-liveness.timer valid."""
    try:
        LIVENESS_FILE.write_text(str(int(time.time())))
    except OSError as e:
        log.warning("liveness write failed: %s", e)


def _sd_notify(state: str) -> None:
    """Send a systemd sd_notify datagram (READY=1, WATCHDOG=1, ...).

    [WHY] Type=notify + WatchdogSec standard: systemd restarts us if a cycle hangs.
    The protocol is stable and reimplementable without libsystemd (sd_notify(3));
    no-op when NOTIFY_SOCKET is unset (e.g. running under CLI/manual).
    """
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    addr = "\0" + sock_path[1:] if sock_path.startswith("@") else sock_path
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError as e:
        log.warning("sd_notify(%s) failed: %s", state, e)


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


@app.command("serve")
def serve() -> None:
    """Watchdog loop entrypoint for the systemd unit (reads WATCHDOG_* env)."""
    asyncio.run(_serve_loop())


async def _serve_loop() -> None:
    if _factory is None:
        raise RuntimeError("watchdog.init() not called from composition root")
    svc = await _factory()  # single event loop
    interval = svc.check_interval_sec
    log.info("watchdog serve: interval=%ss dry_run=%s", interval, svc.dry_run)
    _sd_notify("READY=1")
    while True:
        await svc.run_cycle()
        _write_liveness()
        _sd_notify("WATCHDOG=1")
        await asyncio.sleep(interval)
