"""CLI subcommand for MCP server management."""

from __future__ import annotations

import typer

from devforge.core.logging import get_logger

app = typer.Typer(name="mcp", help="MCP server management")
logger = get_logger(__name__)


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", "--host", "-h"),
    port: int = typer.Option(8100, "--port", "-p"),
) -> None:
    """Start the MCP SSE server."""
    import uvicorn

    from devforge.adapters.driving.mcp.server import app as mcp_app

    logger.info("mcp_server_start", host=host, port=port)

    # Update server configuration
    mcp_app.title = "DevForge MCP Server"

    uvicorn.run(
        "devforge.adapters.driving.mcp.server:app",
        host=host,
        port=port,
        log_level="info",
    )
