#!/bin/bash
# scripts/manual_handover.sh — Interactive handover updater for manual agents
# Usage: ./scripts/manual_handover.sh <agent_name>
#   ./scripts/manual_handover.sh copilot-chat
#   ./scripts/manual_handover.sh copilot-edits

set -e

HANDOVER="/opt/projects/server/handover.yaml"
LOCK_FILE="/opt/projects/server/.handover.lock"
AGENT_NAME="${1:-manual}"

echo "=== Manual Handover Update ==="
echo "Agent: $AGENT_NAME"
echo ""

echo "Current task: $(yq '.current_task.summary' "$HANDOVER")"
echo ""

read -p "Task completed? (y/n): " COMPLETED
if [ "$COMPLETED" = "y" ]; then
    STATUS="completed"
else
    STATUS="in_progress"
fi

read -p "New pending items (comma-separated, or empty): " PENDING_INPUT
read -p "New known issues (comma-separated, or empty): " ISSUES_INPUT

# Get changed files from git
CHANGED_FILES=$(cd /opt/projects && git diff --name-only HEAD 2>/dev/null | head -10 | yq -n '[inputs]' 2>/dev/null || echo '[]')

# Update handover under lock
(
    flock -x 200

    cp "$HANDOVER" "${HANDOVER}.bak"

    yq eval -i "
      .last_checkpoint.time = \"$(date -Iseconds)\" |
      .last_checkpoint.agent = \"$AGENT_NAME\" |
      .last_checkpoint.recent_files = $CHANGED_FILES |
      .current_task.status = \"$STATUS\"
    " "$HANDOVER"

    # Handle pending items
    if [ -n "$PENDING_INPUT" ]; then
        echo "$PENDING_INPUT" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | \
            yq eval '.pending = load("/dev/stdin")' - | \
            yq eval-all 'select(fileIndex == 0) as $base | select(fileIndex == 1) as $patch | $base | .pending = $patch.pending' "$HANDOVER" - > "${HANDOVER}.tmp"
        mv "${HANDOVER}.tmp" "$HANDOVER"
    fi

    # Handle known issues
    if [ -n "$ISSUES_INPUT" ]; then
        echo "$ISSUES_INPUT" | tr ',' '\n' | sed 's/^ *//;s/ *$//' | grep -v '^$' | \
            yq eval '.known_issues = load("/dev/stdin")' - | \
            yq eval-all 'select(fileIndex == 0) as $base | select(fileIndex == 1) as $patch | $base | .known_issues = $patch.known_issues' "$HANDOVER" - > "${HANDOVER}.tmp"
        mv "${HANDOVER}.tmp" "$HANDOVER"
    fi

    echo "[SYSTEM] Handover updated successfully."

) 200>"$LOCK_FILE"

echo ""
echo "Updated state saved. View with: ./status.sh"
