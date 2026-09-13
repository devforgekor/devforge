#!/bin/bash
# Wrapper for Brave Search MCP — extracts API key from secrets.env
set -euo pipefail

SECRETS="$HOME/.config/devforge/secrets.env"
if [ -f "$SECRETS" ]; then
    key_line=$(grep "^BRAVE_API_KEYS=" "$SECRETS" | head -1)
    if [ -n "$key_line" ]; then
        # Extract first key (format: name:key,next)
        raw="${key_line#*=}"
        raw="${raw%\"*}"
        raw="${raw%\'*}"
        part="${raw%%,*}"
        key="${part#*:}"
        export BRAVE_API_KEY="$key"
    fi
fi

exec npx -y @brave/brave-search-mcp-server "$@"
