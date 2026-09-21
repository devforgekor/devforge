#!/bin/bash
# system_sync.sh — 30min system maintenance (no inference, no pipeline)
# Called by devforge-system-sync.timer
# Tasks: duckdns, lightweight housekeeping (gen_architecture retired 2026-09-14)

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }
LOG() { echo "[$(LOG_TS)] $*"; }
SCRIPT_DIR="/opt/projects/server/scripts"

LOG "system_sync start"

# ── duckdns ──
DUCKDNS_TOKEN_KEY="${DUCKDNS_TOKEN_KEY:-}"
if curl -s -o /dev/null -w "%{http_code}" \
    "https://www.duckdns.org/update?domains=devforgekor&token=${DUCKDNS_TOKEN_KEY:-MISSING}&ip=&verbose=true" \
    2>/dev/null | grep -q 200; then
    LOG "  duckdns OK"
else
    LOG "  duckdns FAILED (non-fatal)" >&2
fi

# ── git auto-commit (local only, skip if no changes) ──
cd /opt/projects/server 2>/dev/null || exit 1

# Guard: never touch the index while another git operation is mid-flight.
if [ -e .git/index.lock ] || [ -e .git/MERGE_HEAD ] \
   || [ -e .git/rebase-merge ] || [ -e .git/rebase-apply ]; then
    LOG "  git commit SKIP (git operation in progress)"
else
    # Auto-commit is a safety net for generated/runtime state ONLY.
    # Authored code (src/, tests/, scripts/, pyproject.toml) is excluded on
    # purpose: those must be committed deliberately with a descriptive message.
    # Before 2026-09-21 this step did a bare `git add -A`, which swept a human/
    # agent's in-progress source edits into a generic "auto: sync" commit and
    # raced their explicit commit (e.g. bd41445 absorbed staged Phase 0 review
    # fixes before they could be committed). Scoping the stage set removes that
    # race: source changes stay in the working tree for the author to commit.
    git add -A
    git reset -q -- src tests scripts pyproject.toml 2>/dev/null
    if git diff --cached --quiet; then
        LOG "  git commit SKIP (no non-source changes)"
    else
        git commit -m "auto: sync $(date +%Y-%m-%d)"
        LOG "  git commit OK"
    fi
fi

LOG "system_sync done"
