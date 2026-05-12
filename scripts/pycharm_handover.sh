#!/bin/bash
# scripts/pycharm_handover.sh — PyCharm Junie External Tool target
# Usage: Called from PyCharm External Tools or terminal after Junie session.
# Detects uncommitted changes via git diff and updates handover under flock.

set -e

HANDOVER="/opt/projects/server/handover.yaml"
LOCK_FILE="/opt/projects/server/.handover.lock"
PROJECT_ROOT="/opt/projects"

cd "$PROJECT_ROOT"

CHANGED_FILES=$(git diff --name-only HEAD 2>/dev/null | head -10 | yq -n '[inputs]' 2>/dev/null || echo '[]')

if [ "$CHANGED_FILES" = "[]" ]; then
    echo "[INFO] No uncommitted changes detected."
    exit 0
fi

GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
GIT_COMMIT=$(git rev-parse --short HEAD 2>/dev/null || echo "unknown")

(
    flock -x 200

    cp "$HANDOVER" "${HANDOVER}.bak"

    yq eval -i "
      .last_checkpoint.time = \"$(date -Iseconds)\" |
      .last_checkpoint.agent = \"pycharm-junie\" |
      .last_checkpoint.recent_files = $CHANGED_FILES |
      .last_checkpoint.git.branch = \"$GIT_BRANCH\" |
      .last_checkpoint.git.commit = \"$GIT_COMMIT\"
    " "$HANDOVER"

    echo "[SYSTEM] Handover updated."
    echo "[SYSTEM] Branch: $GIT_BRANCH ($GIT_COMMIT)"

) 200>"$LOCK_FILE"
