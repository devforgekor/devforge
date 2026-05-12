#!/bin/bash
# status.sh — Quick handover status viewer
set -e

HANDOVER="/opt/projects/server/handover.yaml"

if [ ! -f "$HANDOVER" ]; then
    echo "Handover file not found: $HANDOVER"
    exit 1
fi

echo "=== Agent Handover Status ==="
echo ""
echo "Last update: $(yq '.last_checkpoint.time + " by " + .last_checkpoint.agent' "$HANDOVER")"
echo ""
echo "Current task:"
yq '.current_task.summary // "(none)"' "$HANDOVER"
echo "  Status   : $(yq '.current_task.status // "N/A"' "$HANDOVER")"
yq '.current_task.started' "$HANDOVER" | grep -v "^$" | sed 's/^/  Started  : /'
yq '.current_task.result' "$HANDOVER" | grep -v "^$" | sed 's/^/  Result   : /'
echo ""
echo "Pending ($(yq '.pending | length' "$HANDOVER")):"
yq '.pending[]' "$HANDOVER" 2>/dev/null | sed 's/^/  - /' || echo "  (none)"
echo ""
echo "Known issues ($(yq '.known_issues | length' "$HANDOVER")):"
yq '.known_issues[]' "$HANDOVER" 2>/dev/null | sed 's/^/  - /' || echo "  (none)"
echo ""
echo "Completed ($(yq '.completed_log | length' "$HANDOVER")):"
yq '.completed_log[]' "$HANDOVER" 2>/dev/null | sed 's/^/  - /' || echo "  (none)"
