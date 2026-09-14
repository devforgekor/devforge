"""CLI subcommand for inference model management."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import typer

from devforge.core.config import get_config
from devforge.core.logging import get_logger

app = typer.Typer(name="inference", help="Inference model management")
logger = get_logger(__name__)


def _run_cmd(cmd: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


@app.command("switch")
def switch_mode(
    mode: str = typer.Argument(..., help="Mode: day or night"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Switch inference mode (day/night) and restart model pods."""
    config = get_config()
    current = config.system_mode

    if mode not in ("day", "night"):
        typer.echo(f"Error: mode must be 'day' or 'night', got '{mode}'", err=True)
        raise typer.Exit(1)

    if mode == current and not dry_run:
        typer.echo(f"Already in {mode} mode")
        return

    # Update current-system-mode.env
    system_env = config.paths.current_system_mode_env
    if dry_run:
        typer.echo(f"[dry-run] Would write MODE={mode} to {system_env}")
        typer.echo("[dry-run] Would signal day_cycle.sh to restart")
        return

    system_env.write_text(f"MODE={mode}\n")
    typer.echo(f"Switched to {mode} mode (wrote {system_env})")

    # Signal day_cycle.sh
    result = _run_cmd(["pkill", "-USR1", "-f", "day_cycle.sh"])
    if result["returncode"] == 0:
        typer.echo("Sent USR1 signal to day_cycle.sh")
    else:
        typer.echo("Warning: could not signal day_cycle.sh (may not be running)", err=True)


@app.command("status")
def inference_status() -> None:
    """Show current inference model status."""
    config = get_config()
    status = {
        "system_mode": config.system_mode,
        "inference_mode": config.inference_mode,
        "model_name": config.model_name,
        "port": config.model_port,
        "system_mode_env": str(config.paths.current_system_mode_env),
        "mode_env": str(config.paths.current_mode_env),
    }
    typer.echo(json.dumps(status, indent=2))


@app.command("ensure")
def ensure_model(
    model_key: str = typer.Argument(..., help="Model key (e.g., day_extract, day_enricher)"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Ensure the model pod is running for the given model key."""
    from devforge.adapters.driven.llm.local_adapter import MODEL_REGISTRY

    if model_key not in MODEL_REGISTRY:
        typer.echo(f"Error: unknown model '{model_key}'", err=True)
        typer.echo(f"Available: {list(MODEL_REGISTRY)}", err=True)
        raise typer.Exit(1)

    cfg = MODEL_REGISTRY[model_key]
    if "_model" in cfg:
        model_name = cfg["_model"]
        port = MODEL_REGISTRY[model_name]["port"]
    else:
        model_name = model_key
        port = cfg["port"]

    if dry_run:
        typer.echo(f"[dry-run] Would ensure model '{model_name}' on port {port}")
        return

    # Check if port is listening
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    result = sock.connect_ex(("127.0.0.1", port))
    sock.close()

    if result == 0:
        typer.echo(f"✓ Model '{model_name}' is ready on port {port}")
    else:
        typer.echo(f"✗ Model '{model_name}' not responding on port {port}", err=True)
        raise typer.Exit(1)
