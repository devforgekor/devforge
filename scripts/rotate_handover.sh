#!/bin/bash
# scripts/rotate_handover.sh — 3-Tier handover archival
# Hot (handover.yaml) → recent (30d) → archive (90d quarterly) → delete (3yr)
# Run via cron: 0 3 * * * /opt/projects/scripts/rotate_handover.sh

set -euo pipefail
export TZ=UTC

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HANDOVER="${SCRIPT_DIR}/server/handover.yaml"
RECENT="${SCRIPT_DIR}/server/handover_recent.yaml"
ARCHIVE_DIR="${SCRIPT_DIR}/server/archive"
LOCK_FILE="${SCRIPT_DIR}/server/.handover.lock"

CUTOFF_30=$(date -d '30 days ago' +%Y-%m-%dT%H:%M:%SZ)
CUTOFF_90=$(date -d '90 days ago' +%Y-%m-%dT%H:%M:%SZ)

MONTH=$(date +%m)
MONTH=$((10#$MONTH))
QUARTER=$(( (MONTH - 1) / 3 + 1 ))
CURRENT_QUARTER="$(date +%Y)-Q${QUARTER}"

exec 200>"$LOCK_FILE"
flock -w 300 200 || { echo "[FATAL] Lock timeout after 5min."; exit 1; }

[ ! -f "$RECENT" ] && echo 'recent_tasks: []' > "$RECENT"
mkdir -p "$ARCHIVE_DIR"

# Tier 1: handover.yaml → handover_recent.yaml (tasks older than 30 days)
OLD_COUNT=$(yq eval "
  [.completed_log[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_30\")] | length
" "$HANDOVER" 2>/dev/null || echo 0)

if [ "$OLD_COUNT" -gt 0 ]; then
    TMP_OLD=$(mktemp)
    yq eval "
      [.completed_log[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_30\")]
    " "$HANDOVER" > "$TMP_OLD"

    yq eval-all 'select(fileIndex == 0).recent_tasks += (select(fileIndex == 1) | .[])' \
        "$RECENT" "$TMP_OLD" > "${RECENT}.tmp"
    mv "${RECENT}.tmp" "$RECENT"

    yq eval -i ".completed_log |= map(select((.completed // .started // \"9999-12-31T23:59:59Z\") >= \"$CUTOFF_30\"))" "$HANDOVER"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Moved $OLD_COUNT entries to handover_recent.yaml"
    rm -f "$TMP_OLD"
fi

# Tier 2: handover_recent.yaml → archive (tasks older than 90 days)
ARCHIVE_COUNT=$(yq eval "
  [.recent_tasks[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_90\")] | length
" "$RECENT" 2>/dev/null || echo 0)

if [ "$ARCHIVE_COUNT" -gt 0 ]; then
    ARCHIVE_FILE="${ARCHIVE_DIR}/handover_${CURRENT_QUARTER}.yaml"
    [ ! -f "$ARCHIVE_FILE" ] && echo 'archived_tasks: []' > "$ARCHIVE_FILE"

    TMP_ARCHIVE=$(mktemp)
    yq eval "
      [.recent_tasks[] | select((.completed // .started // \"9999-12-31T23:59:59Z\") < \"$CUTOFF_90\")]
    " "$RECENT" > "$TMP_ARCHIVE"

    yq eval-all 'select(fileIndex == 0).archived_tasks += (select(fileIndex == 1) | .[])' \
        "$ARCHIVE_FILE" "$TMP_ARCHIVE" > "${ARCHIVE_FILE}.tmp"
    mv "${ARCHIVE_FILE}.tmp" "$ARCHIVE_FILE"

    yq eval -i ".recent_tasks |= map(select((.completed // .started // \"9999-12-31T23:59:59Z\") >= \"$CUTOFF_90\"))" "$RECENT"
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Archived $ARCHIVE_COUNT entries to $CURRENT_QUARTER"
    rm -f "$TMP_ARCHIVE"
fi

# Tier 3: Delete archives older than 3 years
find "$ARCHIVE_DIR" -name "*.yaml" -mtime +1095 -delete 2>/dev/null || true

exec 200>&-
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Rotation complete"
