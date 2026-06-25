#!/bin/bash
# mcp_entrypoint.sh — Start FastMCP server on Pod A pod
exec python3 /scripts/mcp_server.py --host 0.0.0.0 --port 8000
