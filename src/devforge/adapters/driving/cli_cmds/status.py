"""CLI subcommands for status inspection.

Usage:
    devforge status
"""
from __future__ import annotations

import json
import subprocess

import typer

from devforge.core.config import get_config
from devforge.core.logging import get_logger

app = typer.Typer(name="status", help="Show server component status")
logger = get_logger(__name__)


def _run_cmd(cmd: list[str], timeout: int = 10) -> dict:
    """Run a shell command and return result dict."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except Exception as e:
        return {"returncode": -1, "stdout": "", "stderr": str(e)}


def _get_podman_status() -> dict:
    """Check container status via podman."""
    result = _run_cmd(["podman", "ps", "--format=json"])
    if result["returncode"] != 0:
        return {"status": "error", "error": result["stderr"]}

    try:
        containers = json.loads(result["stdout"])
    except json.JSONDecodeError:
        containers = []

    container_summary = []
    for c in containers:
        ports = c.get("Ports") or []
        container_summary.append({
            "name": c.get("Names", [c.get("Id", "unknown")[:12]][0]),
            "image": c.get("Image", ""),
            "status": "running" if c.get("State") == "running" else c.get("State", "unknown"),
            "ports": [p.get("HostPort", "") for p in ports if p.get("HostPort")],
        })

    return {"status": "ok", "containers": container_summary}


def _get_model_status() -> dict:
    """Check LLM model availability via runtime env."""
    config = get_config()
    runtime = config.runtime
    return {
        "mode": runtime.MODE,
        "model_name": runtime.MODEL_NAME,
        "port": runtime.PORT,
        "model_file": runtime.MODEL_FILE,
        "ctx_size": runtime.CTX_SIZE,
        "threads": runtime.THREADS,
    }


def _get_systemd_services() -> dict:
    """Check systemd user service status."""
    units = [
        "devforge-mcp.service",
        "devforge-pipeline.service",
        "devforge-watchdog.service",
    ]
    services = []
    for unit in units:
        result = _run_cmd(["systemctl", "--user", "is-active", unit])
        services.append({
            "unit": unit,
            "active": result["stdout"] == "active",
            "status": result["stdout"],
        })
    return {"services": services}


def _get_filesystem_status() -> dict:
    """Check disk usage on key mount points."""
    mounts = ["/opt/ai_data", "/opt/projects", "/"]
    usage = []
    for mount in mounts:
        result = _run_cmd(["df", "-h", mount])
        if result["returncode"] == 0:
            parts = result["stdout"].split()
            if len(parts) >= 6:
                usage.append({
                    "mount": mount,
                    "size": parts[1],
                    "used": parts[2],
                    "avail": parts[3],
                    "use_percent": parts[4],
                })
    return {"disk_usage": usage}


def get_system_status() -> str:
    """Return JSON status of all system components."""
    status = {
        "containers": _get_podman_status(),
        "model": _get_model_status(),
        "systemd": _get_systemd_services(),
        "filesystem": _get_filesystem_status(),
    }
    return json.dumps(status, indent=2)


@app.command()
def status_json(
    format: str = "json",
):
    """Show system status as JSON."""
    typer.echo(get_system_status())


@app.callback(invoke_without_command=True)
def status_main(
    ctx: typer.Context,
    json_output: bool = typer.Option(False, "--json", "-j", help="JSON output"),
):
    """Show current server status."""
    if ctx.invoked_subcommand:
        return

    if json_output:
        typer.echo(get_system_status())
    else:
        config = get_config()
        typer.secho("DevForge Server Status", fg="cyan", bold=True)
        typer.echo(f"  Mode: {config.system_mode} (inference: {config.inference_mode})")
        typer.echo(f"  Model: {config.model_name} on port {config.model_port}")
        typer.echo(f"  DB URL: {config.db_url.replace(config.secrets.POSTGRES_PASSWORD, '***') if config.secrets.POSTGRES_PASSWORD else config.db_url}")

        # Container status
        containers = _get_podman_status()
        if containers["status"] == "ok":
            running = sum(1 for c in containers["containers"] if c["status"] == "running")
            total = len(containers["containers"])
            typer.echo(f"  Containers: {running}/{total} running")
        else:
            typer.echo(f"  Containers: {containers.get('error', 'unknown')}")

        typer.echo("\nUse 'devforge status --json' for full JSON output")
