#!/bin/bash
set -euo pipefail

SECRETS="$HOME/.config/devforge/secrets.env"
if [ -f "$SECRETS" ]; then
    key_line=$(grep "^GITHUB_TOKEN=" "$SECRETS" | head -1)
    if [ -n "$key_line" ]; then
        raw="${key_line#*=}"
        raw="${raw%\"*}"
        raw="${raw%\'*}"
        export GITHUB_TOKEN="$raw"
    fi
fi

exec node /home/opc/.local/lib/node_modules/@modelcontextprotocol/server-github/dist/index.js "$@"
