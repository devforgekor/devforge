#!/bin/bash
# system_sync.sh — 30min system maintenance (no inference, no pipeline)
# Called by devforge-system-sync.timer
# Tasks: gen_architecture, duckdns, lightweight housekeeping

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
SCRIPT_DIR="/opt/projects/server/scripts"

LOG "system_sync start"

# ── code-structure (hash-guarded, lightweight) ──
if python3 "$SCRIPT_DIR/gen_architecture.py" --check-structure 2>&1; then
    LOG "  code-structure OK"
else
    LOG "  code-structure FAILED (non-fatal)" >&2
fi

# ── duckdns ──
SECRETS="$HOME/.config/devforge/secrets.env"
DUCKDNS_TOKEN=""
[ -f "$SECRETS" ] && source "$SECRETS"
if curl -s -o /dev/null -w "%{http_code}" \
    "https://www.duckdns.org/update?domains=devforgekor&token=${DUCKDNS_TOKEN:-MISSING}&ip=&verbose=true" \
    2>/dev/null | grep -q 200; then
    LOG "  duckdns OK"
else
    LOG "  duckdns FAILED (non-fatal)" >&2
fi

# ── git auto-commit (local only, skip if no changes) ──
cd /opt/projects/server 2>/dev/null || exit 1
if git status --porcelain | grep -q .; then
    git add -A
    git commit -m "auto: sync $(date +%Y-%m-%d)"
    LOG "  git commit OK"
else
    LOG "  git commit SKIP (no changes)"
fi

LOG "system_sync done"
