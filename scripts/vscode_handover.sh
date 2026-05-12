#!/bin/bash
# scripts/vscode_handover.sh — Auto-update handover on file changes (VSCode task)
# Called by VSCode tasks.json or keybinding.
# Uses non-blocking flock: if another agent is active, silently skip.

HANDOVER="/opt/projects/server/handover.yaml"
LOCK_FILE="/opt/projects/server/.handover.lock"
PROJECT_ROOT="/opt/projects"

cd "$PROJECT_ROOT"

# Only update if there are uncommitted changes
if ! git diff --quiet HEAD 2>/dev/null; then
    (
        flock -n 200 || exit 0  # Non-blocking: skip if another agent is active

        yq eval -i "
          .last_checkpoint.time = \"$(date -Iseconds)\" |
          .last_checkpoint.agent = \"vscode-autosave\" |
          .last_checkpoint.recent_files = $(git diff --name-only HEAD | head -5 | yq -n '[inputs]' 2>/dev/null || echo '[]')
        " "$HANDOVER"

    ) 200>"$LOCK_FILE"
fi
