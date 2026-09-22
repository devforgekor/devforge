#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, systemd
"""CLI subcommands for watchdog operations."""
from __future__ import annotations

import asyncio
from typing import Any

import typer

app = typer.Typer(name="watchdog", help="Watchdog operations")

# Composition root injection (set by cli.py)
_service: Any = None  # WatchdogService


def init(service: Any) -> None:
    """Set the watchdog service (called from composition root)."""
    global _service
    _service = service


def _get_service() -> Any:
    """Get the watchdog service (requires composition root init)."""
    global _service
    if _service is None:
        raise RuntimeError(
            "Watchdog service not initialized. Call watchdog.init() from cli.py first."
        )
    return _service


@app.command("status")
def watchdog_status() -> None:
    """Show component status."""
    service = _get_service()
    statuses = service.get_all_statuses()

    for status in statuses:
        typer.echo(
            f"{status.name}: {status.state.value} "
            f"(fail_count={status.fail_count}, "
            f"circuit={'OPEN' if status.circuit_open else 'CLOSED'})"
        )


@app.command("check")
def watchdog_check() -> None:
    """Run one check-fix cycle."""
    service = _get_service()
    result = asyncio.run(service.run_check_cycle())

    typer.echo(f"Checks: {result['checks']}")
    typer.echo(f"Failed: {result['failed']}")
    typer.echo(f"Fixed: {result['fixed']}")
    typer.echo(f"Timestamp: {result['timestamp']}")


@app.command("resolve")
def watchdog_resolve(
    incident_id: int = typer.Argument(..., help="Incident ID"),
    note: str = typer.Argument(..., help="Resolution note"),
) -> None:
    """Resolve an incident."""
    service = _get_service()
    success = asyncio.run(service.resolve_incident(incident_id, note))

    if success:
        typer.echo(f"Incident {incident_id} resolved")
    else:
        typer.echo(f"Failed to resolve incident {incident_id}")
