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

# ── git safety-net snapshot (hidden ref, never touches main history) ──
cd /opt/projects/server 2>/dev/null || exit 1

# Guard: never touch the index while another git operation is mid-flight,
# and never disturb an author's staged work (index must be clean before add).
if [ -e .git/index.lock ] || [ -e .git/MERGE_HEAD ] \
   || [ -e .git/rebase-merge ] || [ -e .git/rebase-apply ] \
   || ! git diff --cached --quiet 2>/dev/null; then
    LOG "  snapshot SKIP (git busy or staged work present)"
else
    # Crash safety net ONLY: snapshot the whole worktree into a hidden ref.
    # main history stays intent-only — commits happen at commit-times by the
    # author (AGENTS.md §9). The hidden ref keeps the "auto: sync" 62% log
    # pollution out of `git log` while preserving a 30-min restore point.
    # Restore: git show refs/snapshots/sync -- <path>
    #          git checkout refs/snapshots/sync -- <path>
    git add -A
    if git diff --cached --quiet; then
        git reset -q
        LOG "  snapshot SKIP (no changes)"
    else
        tree=$(git write-tree)
        snap=$(git commit-tree "$tree" -p HEAD \
            -m "auto snapshot $(date -u +%Y-%m-%dT%H:%M:%SZ)")
        git update-ref refs/snapshots/sync "$snap"
        git reset -q
        LOG "  snapshot OK ($snap)"
    fi
fi

LOG "system_sync done"
