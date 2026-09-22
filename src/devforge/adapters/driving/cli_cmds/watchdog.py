#!/usr/bin/env python3
# Status: experimental
# Path: cli.py, systemd
"""CLI subcommands for watchdog operations."""
from __future__ import annotations

import asyncio

import typer

app = typer.Typer(name="watchdog", help="Watchdog operations")


@app.command("status")
def watchdog_status() -> None:
    """Show component status."""
    async def _run() -> None:
        typer.echo("Watchdog status: (composition root not yet wired)")
    asyncio.run(_run())


@app.command("check")
def watchdog_check() -> None:
    """Run one check-fix cycle."""
    async def _run() -> None:
        typer.echo("Watchdog check: (composition root not yet wired)")
    asyncio.run(_run())


@app.command("resolve")
def watchdog_resolve(
    incident_id: int = typer.Argument(..., help="Incident ID"),
    note: str = typer.Argument(..., help="Resolution note"),
) -> None:
    """Resolve an incident."""
    async def _run() -> None:
        typer.echo(f"Resolve incident {incident_id}: {note} (placeholder)")
    asyncio.run(_run())
