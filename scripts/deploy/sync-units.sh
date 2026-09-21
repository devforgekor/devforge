#!/bin/bash
# Status: production
# Path: manual / optional preflight (see docs/plans/phase1-plan.md §0.1)
# sync-units.sh — deploy the version-controlled unit mirrors to the live systemd config.
#
# Why: unit definitions live in two places — the version-controlled mirror
# (containers/systemd/, systemd/user/) and the live paths (~/.config/...).
# Mirrors can drift from live when one side is edited by hand. This script
# makes the repo the source of truth and provides a drift check for timers/CI.
#
# Usage:
#   sync-units.sh          # copy repo mirrors -> live paths, then daemon-reload
#   sync-units.sh --check  # report drift only (no changes); exit 1 if drift
set -uo pipefail

REPO_ROOT="/opt/projects/server"
LIVE_CONTAINERS="$HOME/.config/containers/systemd"
LIVE_USER="$HOME/.config/systemd/user"
MODE="${1:-sync}"

drift=0

report() {
    if [ ! -e "$2" ]; then
        echo "MISSING in live: $2"
        drift=1
    elif ! diff -q "$1" "$2" >/dev/null 2>&1; then
        echo "DRIFT: $1 -> $2"
        drift=1
    fi
}

# Compare every managed mirror file against its live counterpart.
for f in "$REPO_ROOT"/containers/systemd/*.container "$REPO_ROOT"/containers/systemd/*.pod; do
    [ -e "$f" ] || continue
    report "$f" "$LIVE_CONTAINERS/$(basename "$f")"
done
for f in "$REPO_ROOT"/systemd/user/*.service "$REPO_ROOT"/systemd/user/*.timer; do
    [ -e "$f" ] || continue
    report "$f" "$LIVE_USER/$(basename "$f")"
done

if [ "$MODE" = "--check" ]; then
    if [ "$drift" -eq 0 ]; then
        echo "units: no drift"
    else
        echo "units: DRIFT detected (run sync-units.sh to deploy)"
    fi
    exit "$drift"
fi

if [ "$drift" -eq 0 ] && [ "${FORCE:-0}" != "1" ]; then
    echo "units: already in sync (set FORCE=1 to copy anyway)"
    exit 0
fi

# Copy only unit files (never the READMEs) so we do not litter ~/.config.
for f in "$REPO_ROOT"/containers/systemd/*.container "$REPO_ROOT"/containers/systemd/*.pod; do
    [ -e "$f" ] && cp -p "$f" "$LIVE_CONTAINERS/"
done
for f in "$REPO_ROOT"/systemd/user/*.service "$REPO_ROOT"/systemd/user/*.timer; do
    [ -e "$f" ] && cp -p "$f" "$LIVE_USER/"
done

systemctl --user daemon-reload
echo "units: synced repo -> live + daemon-reload ($(date -u +%Y-%m-%dT%H:%M:%SZ))"
