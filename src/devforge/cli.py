"""DevForge CLI — single entry point for all server operations.

Usage:
    devforge --help
    devforge status
    devforge pipeline orchestrate
    devforge pipeline status
    devforge mcp serve
    devforge inference switch day
    devforge inference status
"""
from __future__ import annotations

import typer

from devforge.core.logging import setup_logging

setup_logging(level="INFO", component="cli")

app = typer.Typer(
    name="devforge",
    help="DevForge: AI agent workflow pipeline server",
    no_args_is_help=True,
)

# ── Sub-apps ──
pipeline_app = typer.Typer(name="pipeline", help="Pipeline management")
mcp_app = typer.Typer(name="mcp", help="MCP server management")
inference_app = typer.Typer(name="inference", help="Inference model management")

app.add_typer(pipeline_app, name="pipeline")
app.add_typer(mcp_app, name="mcp")
app.add_typer(inference_app, name="inference")


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
):
    """Show current server status (containers, models, timers, services)."""
    from devforge.core.config import get_config
    config = get_config()

    if json_output:
        from devforge.adapters.driving.cli_cmds.status import get_system_status
        typer.echo(get_system_status())
    else:
        typer.secho("DevForge Server Status", fg="cyan", bold=True)
        typer.echo(f"  Mode: {config.system_mode} (inference: {config.inference_mode})")
        typer.echo(f"  Model: {config.model_name} on port {config.model_port}")
        typer.echo(f"  DB URL: {config.db_url[:60]}...")
        typer.echo("  Use 'devforge status --json' for full JSON output")


@pipeline_app.command("orchestrate")
def pipeline_orchestrate(
    limit: int = typer.Option(50, "--limit", "-n", help="Max turns to process"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate without writing"),
    turn_id: str = typer.Option(None, "--turn-id", help="Process single turn"),
):
    """Run the extract pipeline on unprocessed turns."""
    import asyncio
    import json as json_module
    from uuid import UUID

    from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter
    from devforge.adapters.driven.storage.extract_adapter import (
        PostgresExtractAdapter,
        PostgresTurnRepository,
    )
    from devforge.application.extract_pipeline import ExtractPipeline
    from devforge.core.config import get_config

    config = get_config()
    pipeline = ExtractPipeline(
        llm=LocalLLMAdapter(),
        db=PostgresExtractAdapter.from_config(config),
        turn_repo=PostgresTurnRepository.from_config(config),
    )
    pipeline._dry_run = dry_run

    async def run():
        if turn_id:
            result = await pipeline.run_single(UUID(turn_id))
            typer.echo(json_module.dumps({
                "success": result.success,
                "facts_extracted": result.facts_extracted,
                "errors": result.errors,
            }, indent=2))
        else:
            results = await pipeline.run_batch(limit=limit)
            summary = {
                "total": len(results),
                "success": sum(1 for r in results if r.success),
                "failed": sum(1 for r in results if not r.success),
                "facts_extracted": sum(r.facts_extracted for r in results),
                "elapsed_ms_total": sum(r.elapsed_ms for r in results),
            }
            typer.echo(json_module.dumps(summary, indent=2))

    asyncio.run(run())


@pipeline_app.command("status")
def pipeline_status_cmd():
    """Show pipeline state distribution."""
    import asyncio
    import json as json_module

    from devforge.adapters.driving.mcp.server import pipeline_status as pipeline_status_fn
    from devforge.ports.extract import PipelineStatusParams

    async def run():
        try:
            result = await pipeline_status_fn(PipelineStatusParams())
            typer.echo(json_module.dumps(result, indent=2, default=str))
        except Exception as e:
            typer.echo(json_module.dumps({
                "error": f"Cannot connect to database: {e}",
                "use": "devforge pipeline orchestrate --dry-run for simulation",
            }, indent=2))

    asyncio.run(run())


@mcp_app.command("serve")
def mcp_serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),
    port: int = typer.Option(8100, "--port", "-p"),
):
    """Start the MCP SSE server."""
    import uvicorn

    from devforge.adapters.driving.api.app import create_app
    app = create_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )


@inference_app.command("switch")
def inference_switch(
    mode: str = typer.Argument(..., help="Mode: day or night"),
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    """Switch inference mode (day/night) and restart model pods."""
    import subprocess

    from devforge.core.config import get_config

    config = get_config()
    if mode not in ("day", "night"):
        typer.echo(f"Error: mode must be 'day' or 'night', got '{mode}'", err=True)
        raise typer.Exit(1)

    system_env = config.paths.current_system_mode_env
    if dry_run:
        typer.echo(f"[dry-run] Would write MODE={mode} to {system_env}")
        return

    system_env.write_text(f"MODE={mode}\n")
    typer.echo(f"Switched to {mode} mode (wrote {system_env})")

    result = subprocess.run(["pkill", "-USR1", "-f", "day_cycle.sh"], capture_output=True)
    if result.returncode == 0:
        typer.echo("Sent USR1 signal to day_cycle.sh")
    else:
        typer.echo("Warning: could not signal day_cycle.sh (may not be running)", err=True)


@inference_app.command("status")
def inference_status():
    """Show current inference model status."""
    import json as json_module

    from devforge.core.config import get_config
    config = get_config()
    status = {
        "system_mode": config.system_mode,
        "inference_mode": config.inference_mode,
        "model_name": config.model_name,
        "port": config.model_port,
    }
    typer.echo(json_module.dumps(status, indent=2))


@inference_app.command("ensure")
def inference_ensure(
    model_key: str = typer.Argument(..., help="Model key (e.g., day_extract, day_enricher)"),
):
    """Ensure the model pod is running for the given model key."""
    import socket

    from devforge.adapters.driven.llm.local_adapter import MODEL_REGISTRY

    if model_key not in MODEL_REGISTRY:
        typer.echo(f"Error: unknown model '{model_key}'", err=True)
        typer.echo(f"Available: {list(MODEL_REGISTRY)}", err=True)
        raise typer.Exit(1)

    cfg = MODEL_REGISTRY[model_key]
    model_name = cfg.get("_model", model_key)
    if model_name != model_key:
        cfg = MODEL_REGISTRY[model_name]
    port = cfg["port"]

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    result = sock.connect_ex(("127.0.0.1", port))
    sock.close()

    if result == 0:
        typer.echo(f"✓ Model '{model_name}' is ready on port {port}")
    else:
        typer.echo(f"✗ Model '{model_name}' not responding on port {port}", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
