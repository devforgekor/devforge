"""CLI subcommands for pipeline operations.

Usage:
    devforge pipeline orchestrate [--limit 50] [--dry-run]
    devforge pipeline status
    devforge pipeline extract <turn-id>
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import typer

from devforge.application.extract_pipeline import ExtractPipeline
from devforge.core.config import get_config
from devforge.core.logging import get_logger

app = typer.Typer(name="pipeline", help="Pipeline orchestration commands")
logger = get_logger(__name__)


def _get_pipeline() -> ExtractPipeline:
    """Create an ExtractPipeline with real deps."""
    from devforge.adapters.driven.llm.local_adapter import LocalLLMAdapter
    from devforge.adapters.driven.storage.extract_adapter import (
        PostgresExtractAdapter, PostgresTurnRepository,
    )
    config = get_config()
    return ExtractPipeline(
        llm=LocalLLMAdapter(),
        db=PostgresExtractAdapter.from_config(config),
        turn_repo=PostgresTurnRepository.from_config(config),
    )


@app.command("orchestrate")
def orchestrate(
    limit: int = typer.Option(50, "--limit", "-n", help="Max turns to process"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simulate without writing"),
    turn_id: Optional[str] = typer.Option(None, "--turn-id", help="Process single turn"),
):
    """Run the extract pipeline on unprocessed turns."""
    pipeline = _get_pipeline()
    pipeline._dry_run = dry_run

    async def run():
        if turn_id:
            result = await pipeline.run_single(uuid.UUID(turn_id))
            typer.echo(json.dumps({
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
            typer.echo(json.dumps(summary, indent=2))

    asyncio.run(run())


@app.command("status")
def status_cmd():
    """Show pipeline state distribution."""
    from devforge.adapters.driving.mcp.server import pipeline_status
    from devforge.ports.extract import PipelineStatusParams

    async def run():
        result = await pipeline_status(PipelineStatusParams())
        typer.echo(json.dumps(result, indent=2, default=str))

    asyncio.run(run())
