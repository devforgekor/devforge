"""DevForge CLI — single entry point for all server operations.

Usage:
    devforge --help
    devforge pipeline orchestrate
    devforge watchdog status
    devforge mcp serve
    devforge inference switch day
    devforge turn-watcher start
"""
import sys
from pathlib import Path

import typer

app = typer.Typer(
    name="devforge",
    help="DevForge: AI agent workflow pipeline server",
    no_args_is_help=True,
)

# ── Sub-apps for each domain (lazy import to avoid circular deps) ──

def _add_subapp(name: str, import_path: str):
    """Lazy-import sub-app to avoid import cycles during early startup."""
    try:
        module = __import__(import_path, fromlist=["app"])
        sub_app = getattr(module, "app", None)
        if sub_app:
            app.add_typer(sub_app, name=name)
    except ImportError:
        pass  # Sub-app not yet implemented


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
):
    """DevForge server management CLI."""
    if version:
        typer.echo("devforge 1.4.0")
        raise typer.Exit()


@app.command()
def status():
    """Show current server status (containers, models, timers, services)."""
    from devforge.core.logging import get_logger
    
    logger = get_logger()
    logger.info("status_check", message="Querying system status...")
    
    # Lazy import for status collection
    try:
        from devforge.adapters.driving.cli_cmds.status import get_system_status
        status = get_system_status()
        typer.echo(status)
    except ImportError:
        typer.echo("Status module not yet migrated. Use legacy: python3 scripts/cli.py status --json")


# ── Register sub-apps (will silently skip if not implemented yet) ──
_add_subapp("pipeline", "devforge.adapters.driving.cli_cmds.pipeline")
_add_subapp("watchdog", "devforge.adapters.driving.cli_cmds.watchdog")
_add_subapp("mcp", "devforge.adapters.driving.cli_cmds.mcp")
_add_subapp("inference", "devforge.adapters.driving.cli_cmds.inference")
_add_subapp("turn-watcher", "devforge.adapters.driving.cli_cmds.turn_watcher")


if __name__ == "__main__":
    app()
