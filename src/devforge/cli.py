"""DevForge CLI — single entry point for all server operations.

Usage:
    devforge --help
    devforge status
    devforge pipeline extract
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

from devforge.adapters.driving.cli_cmds import errors as errors_cmds
from devforge.adapters.driving.cli_cmds import inference as inference_cmds
from devforge.adapters.driving.cli_cmds import mcp as mcp_cmds
from devforge.adapters.driving.cli_cmds import watchdog as watchdog_cmds
from devforge.adapters.driving.mcp.server import set_pipeline_factory
from devforge.core.logging import setup_logging
from devforge.core.telemetry import setup_telemetry

setup_logging(level="INFO", component="cli")
setup_telemetry(service_name="devforge-cli")

app = typer.Typer(
    name="devforge",
    help="DevForge: AI agent workflow pipeline server",
    no_args_is_help=True,
)

# ── Sub-apps ──
# Pipeline commands are defined here (the CLI is the composition root and is
# allowed to import the application layer); mcp/inference/watchdog are driving
# adapters.
pipeline_app = typer.Typer(name="pipeline", help="Pipeline management")

app.add_typer(pipeline_app, name="pipeline")
app.add_typer(inference_cmds.app, name="inference")
app.add_typer(mcp_cmds.app, name="mcp")
app.add_typer(watchdog_cmds.app, name="watchdog")
app.add_typer(errors_cmds.app, name="errors")


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


async def _watchdog_service_factory() -> Any:
    """Build the production WatchdogService (composition-root wiring).

    Defined here so the watchdog driving adapter never imports the application
    layer directly (see the `layering` import-linter contract).
    """
    import os

    from devforge.application.watchdog_service import create_watchdog_service
    from devforge.core.config import WatchdogConfig

    dry_run = os.environ.get("WATCHDOG_DRY_RUN", "") == "1"
    return create_watchdog_service(WatchdogConfig.from_env(), dry_run=dry_run)


watchdog_cmds.init(_watchdog_service_factory)


def _error_analysis_factory() -> Any:
    """Build the production ErrorAnalysisService (composition-root wiring)."""
    from devforge.adapters.driven.llm.reasoning_hypothesis import ReasoningHypothesisClient
    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.adapters.driven.storage.error_analysis_pg import (
        PostgresErrorAnalysisRepository,
    )
    from devforge.application.error_analysis import ErrorAnalysisService
    from devforge.core.config import get_config

    return ErrorAnalysisService(
        PostgresErrorAnalysisRepository(DatabaseGateway.from_config(get_config())),
        ReasoningHypothesisClient(),
    )


errors_cmds.init(_error_analysis_factory)


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


@pipeline_app.command("extract")
def pipeline_extract(
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


def _build_embed_stage(limit: int, shadow: bool = False, shadow_since: str | None = None) -> Any:
    """Compose the production EmbedStage (composition-root wiring)."""
    from devforge.adapters.driven.llm.embed_adapter import HttpEmbedClient
    from devforge.adapters.driven.storage.database_gateway import DatabaseGateway
    from devforge.adapters.driven.storage.embedding_adapter import PostgresEmbedAdapter
    from devforge.adapters.driven.text.cleaner_splitter import estimate_tokens, split_sentences
    from devforge.core.config import get_config
    from devforge.pipeline_stages.embed import EmbedStage
    from devforge.ports.embed import EmbedPort

    config = get_config()
    gateway = DatabaseGateway.from_config(config)
    port: EmbedPort
    if shadow:
        from devforge.adapters.driven.storage.embedding_shadow_adapter import (
            PostgresEmbedShadowAdapter,
        )

        port = PostgresEmbedShadowAdapter(gateway, since=shadow_since)
    else:
        port = PostgresEmbedAdapter(gateway)
    return EmbedStage(
        port=port,
        client=HttpEmbedClient(),
        split_sentences=split_sentences,
        estimate_tokens=estimate_tokens,
        limit=limit,
    )


def _fts5_refresh_callable() -> Any:
    """Subprocess-backed FTS5 refresh (scripts/pipelines/fts5_refresh.py)."""

    def run() -> str:
        import subprocess

        proc = subprocess.run(
            ["python3.12", "/opt/projects/server/scripts/pipelines/fts5_refresh.py"],
            capture_output=True,
            text=True,
            timeout=150,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"fts5_refresh exit={proc.returncode}: {proc.stderr[-200:]}")
        return "fts5_refresh OK"

    return run


@pipeline_app.command("orchestrate")
def pipeline_orchestrate(
    limit: int = typer.Option(50, "--limit", "-n", help="Max turns to embed"),
    budget_sec: int = typer.Option(3600, "--budget-sec", help="Shared stage budget"),
    skip_fts5: bool = typer.Option(False, "--skip-fts5", help="Skip the FTS5 refresh stage"),
    shadow: bool = typer.Option(
        False, "--shadow", help="Reproject prod embeds into devforge_shadow (verification only)"
    ),
    shadow_since: str = typer.Option(
        None,
        "--shadow-since",
        help="ISO timestamp — only reproject prod embeds created at/after this time (parity window)",
    ),
) -> None:
    """Run owned day-cycle stages (D6=A: FTS5 refresh + embed, enriched -> embedded)."""
    import json as json_module

    from devforge.application.day_cycle import run_full_cycle
    from devforge.application.orchestrator import PipelineBudgetError

    try:
        results = run_full_cycle(
            embed_stage=_build_embed_stage(limit, shadow=shadow, shadow_since=shadow_since),
            # [WHY] FTS5 refresh mutates the prod local index — pointless and
            # forbidden in shadow verification mode.
            fts5_refresh=None if (skip_fts5 or shadow) else _fts5_refresh_callable(),
            budget_sec=budget_sec,
        )
    except PipelineBudgetError as exc:
        typer.echo(json_module.dumps({"error": str(exc)}, indent=2))
        raise typer.Exit(code=1) from exc
    typer.echo(
        json_module.dumps({"mode": "shadow" if shadow else "prod", "stages": results}, indent=2)
    )


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
                        "use": "devforge pipeline extract --dry-run for simulation",
                    },
                    indent=2,
                )
            )

    asyncio.run(run())


if __name__ == "__main__":
    app()
