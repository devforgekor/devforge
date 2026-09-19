#!/bin/bash
set -euo pipefail

export MY_GITHUB_TOKEN_KEY="${MY_GITHUB_TOKEN_KEY:-}"

exec node /home/opc/.local/lib/node_modules/@modelcontextprotocol/server-github/dist/index.js "$@"
