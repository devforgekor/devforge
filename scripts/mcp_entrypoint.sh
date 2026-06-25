#!/bin/bash
# mcp_entrypoint.sh — Start FastMCP server on Pod A pod
exec python3 /scripts/mcp_server.py --port 8000
