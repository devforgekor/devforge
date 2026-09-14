"""DevForge CLI — single entry point for all server operations.

Usage:
    devforge --help
    devforge status
    devforge pipeline orchestrate
    devforge pipeline status
    devforge mcp serve
    devforge inference switch day
    devforge inference status
    devforge inference ensure day_extract
"""

from __future__ import annotations

from typing import Any

import typer

from devforge.adapters.driving.cli_cmds import inference as inference_cmds
from devforge.adapters.driving.cli_cmds import mcp as mcp_cmds
from devforge.adapters.driving.mcp.server import set_pipeline_factory
from devforge.core.logging import setup_logging

setup_logging(level="INFO", component="cli")

app = typer.Typer(
    name="devforge",
    help="DevForge: AI agent workflow pipeline server",
    no_args_is_help=True,
)

# ── Sub-apps ──
# Pipeline commands are defined here (the CLI is the composition root and is
# allowed to import the application layer); mcp/inference are driving adapters.
pipeline_app = typer.Typer(name="pipeline", help="Pipeline management")

app.add_typer(pipeline_app, name="pipeline")
app.add_typer(inference_cmds.app, name="inference")
app.add_typer(mcp_cmds.app, name="mcp")


def _default_pipeline_factory() -> Any:
    """Build the production ExtractPipeline (composition-root wiring).

    Defined here (not in an adapter) so the MCP driving adapter never imports
    the application layer directly.
    """
    from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter
    from devforge.adapters.driven.storage.extract_adapter import (
        PostgresExtractAdapter,
        PostgresTurnRepository,
    )
    from devforge.application.extract_pipeline import ExtractPipeline
    from devforge.core.config import get_config

    config = get_config()
    return ExtractPipeline(
        llm=LocalLLMAdapter(),
        db=PostgresExtractAdapter.from_config(config),
        turn_repo=PostgresTurnRepository.from_config(config),
    )


# Register at import time so `devforge mcp serve` has the factory wired.
set_pipeline_factory(_default_pipeline_factory)


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
) -> None:
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
) -> None:
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
        dry_run=dry_run,
    )

    async def run() -> None:
        if turn_id:
            result = await pipeline.run_single(UUID(turn_id))
            typer.echo(
                json_module.dumps(
                    {
                        "success": result.success,
                        "facts_extracted": result.facts_extracted,
                        "errors": result.errors,
                    },
                    indent=2,
                )
            )
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
def pipeline_status_cmd() -> None:
    """Show pipeline state distribution."""
    import asyncio
    import json as json_module

    from devforge.adapters.driving.mcp.server import pipeline_status as pipeline_status_fn
    from devforge.ports.extract import PipelineStatusParams

    async def run() -> None:
        try:
            result = await pipeline_status_fn(PipelineStatusParams())
            typer.echo(json_module.dumps(result, indent=2, default=str))
        except Exception as e:
            typer.echo(
                json_module.dumps(
                    {
                        "error": f"Cannot connect to database: {e}",
                        "use": "devforge pipeline orchestrate --dry-run for simulation",
                    },
                    indent=2,
                )
            )

    asyncio.run(run())


if __name__ == "__main__":
    app()
