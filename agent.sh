#!/bin/bash
set -eo pipefail
umask 077
export TZ=UTC

# === agent.sh — Multi-Agent Handover Wrapper ===
# Wraps any LLM CLI agent, captures output, extracts handover patch,
# validates YAML syntax, and merges into server/handover.yaml under flock.
#
# Usage:
#   ./agent.sh <agent_name> -- <command> [args...]
#
# Examples:
#   ./agent.sh aider -- aider --model gemini/gemini-2.5-flash
#   ./agent.sh gemini -- gemini -p "Fix the bug in db.py"
#   ./agent.sh qwen -- qwen -p "Review app/config.py"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HANDOVER_FILE="${SCRIPT_DIR}/server/handover.yaml"
LOCK_FILE="${SCRIPT_DIR}/server/.handover.lock"
LOG_DIR="${SCRIPT_DIR}/server/logs"
CURRENT_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

mkdir -p "$LOG_DIR"

# === Cleanup trap (runs on EXIT, ensures lock fd is released) ===
cleanup() {
    exec 200>&- 2>/dev/null || true
}
trap cleanup EXIT

# === 1. Argument Parsing ===
if [ "$#" -lt 3 ] || [ "$2" != "--" ]; then
    echo "Usage: $0 <agent_name> -- <command> [args...]"
    echo "Example: $0 aider -- aider --model gemini/gemini-2.5-flash"
    exit 1
fi

AGENT_NAME="$1"
shift 2
AGENT_ARGS=("$@")   # Array preserves arguments with spaces

# LOG_FILE uses agent name + UTC timestamp (sortable, identifiable)
LOG_FILE="${LOG_DIR}/agent_handover_${AGENT_NAME}_$(date -u +%Y-%m-%dT%H-%M-%SZ).jsonl"
RAW_LOG="${LOG_DIR}/.raw_$$.tmp"

# JSONL line wrapper (one JSON record per output line)
to_jsonl() {
    while IFS= read -r line; do
        printf '{"ts":"%s","agent":"%s","msg":"%s"}\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
            "$1" \
            "$(printf '%s' "$line" | sed 's/\\/\\\\/g; s/"/\\"/g; s/'$'\t''/\\t/g')"
    done
}

# === 2. Pre-flight Checks ===
if [ ! -f "$HANDOVER_FILE" ]; then
    echo "[FATAL] Handover file not found: $HANDOVER_FILE"
    exit 1
fi

if ! command -v yq &>/dev/null; then
    echo "[FATAL] yq (mikefarah/go) is required. Install: go install github.com/mikefarah/yq/v4@latest"
    exit 1
fi

if ! yq --version 2>&1 | grep -q "mikefarah"; then
    echo "[FATAL] Wrong yq installed. Need mikefarah/yq (Go), not kislyuk/yq (Python)."
    echo "Fix: go install github.com/mikefarah/yq/v4@latest"
    exit 1
fi

echo "=== Agent Handover System ==="
echo "Agent : $AGENT_NAME"
echo "Time  : $CURRENT_TIME"
echo "Lock  : $LOCK_FILE"
echo "Log   : $LOG_FILE"
echo ""

# === 3. Execute Agent (capture all output) ===
echo "[SYSTEM] Starting agent: ${AGENT_ARGS[*]}"
echo ""

set +e
# Raw log for patch extraction (temp), JSONL for archival (persisted)
if command -v stdbuf &>/dev/null; then
    stdbuf -oL -eL "${AGENT_ARGS[@]}" 2>&1 | tee "$RAW_LOG" | to_jsonl "$AGENT_NAME" > "$LOG_FILE"
else
    "${AGENT_ARGS[@]}" 2>&1 | tee "$RAW_LOG" | to_jsonl "$AGENT_NAME" > "$LOG_FILE"
fi
AGENT_EXIT=$?
set -e

echo ""
echo "[SYSTEM] Agent exited with code: $AGENT_EXIT"

# === 4. Extract Handover Patch (single awk pass) ===
PATCH_CONTENT=$(awk '
    /^[[:blank:]]*__HANDOVER_PATCH__[[:blank:]]*$/  { flag=1; next }
    /^[[:blank:]]*__END_HANDOVER_PATCH__[[:blank:]]*$/ { exit }
    flag
' "$RAW_LOG" | grep -v '```')

if [ -z "$PATCH_CONTENT" ]; then
    echo "[SYSTEM] No handover patch found. Handover file unchanged."
    echo "[SYSTEM] JSONL log preserved at: $LOG_FILE"
    exit $AGENT_EXIT
fi

echo "$PATCH_CONTENT" > "${RAW_LOG}.patch"
echo "[SYSTEM] Patch extracted: $(wc -l < "${RAW_LOG}.patch") lines"

# === 5. YAML Syntax Validation (before acquiring lock) ===
if ! yq eval '.' "${RAW_LOG}.patch" > /dev/null 2>&1; then
    echo "[FATAL] Patch YAML syntax is INVALID. Handover file NOT modified."
    echo "[FATAL] yq error:"
    yq eval '.' "${RAW_LOG}.patch" 2>&1 || true
    echo ""
    echo "[SYSTEM] Patch content preserved at: ${RAW_LOG}.patch"
    echo "[SYSTEM] Full log preserved at: $LOG_FILE"
    echo "[SYSTEM] Fix the YAML syntax and re-run, or manually edit handover.yaml"
    exit 1
fi

echo "[SYSTEM] YAML syntax validation: PASSED"

# === 6. Acquire Lock and Merge ===
exec 200>"$LOCK_FILE"

echo "[SYSTEM] Acquiring lock..."
flock -w 300 200 || { echo "[FATAL] Lock timeout after 5min."; exit 1; }

echo "[SYSTEM] Lock acquired."

cp "$HANDOVER_FILE" "${HANDOVER_FILE}.bak"

yq eval -i ".last_checkpoint.time = \"$CURRENT_TIME\"" "${RAW_LOG}.patch"
yq eval -i ".last_checkpoint.agent = \"$AGENT_NAME\"" "${RAW_LOG}.patch"

# Explicit merge: arrays = full replace, objects = deep merge.
# Only keys listed here are merged (safe allowlist).
# Agents MUST output complete arrays, not partial additions.
# Arrays are replaced, not appended — this is intentional (declarative state).
yq eval-all '
  select(fileIndex == 0) as $base |
  select(fileIndex == 1) as $patch |
  $base |
  .pending = ($patch.pending // $base.pending) |
  .decisions = ($patch.decisions // $base.decisions) |
  .known_issues = ($patch.known_issues // $base.known_issues) |
  .completed_log = ($patch.completed_log // $base.completed_log) |
  .current_task = ($base.current_task * ($patch.current_task // {})) |
  .last_checkpoint = ($base.last_checkpoint * ($patch.last_checkpoint // {}))
' "$HANDOVER_FILE" "${RAW_LOG}.patch" > "${HANDOVER_FILE}.tmp"

# Post-merge validation
if ! yq eval '.' "${HANDOVER_FILE}.tmp" > /dev/null 2>&1; then
    echo "[FATAL] Merged YAML is INVALID. Rolling back."
    echo "[FATAL] Backup preserved at: ${HANDOVER_FILE}.bak"
    echo "[FATAL] Debug files: ${RAW_LOG}.patch, ${HANDOVER_FILE}.tmp"
    rm -f "${HANDOVER_FILE}.tmp"
    exit 1
fi

mv "${HANDOVER_FILE}.tmp" "$HANDOVER_FILE"

# === 7. Cap completed_log to prevent unbounded growth ===
MAX_COMPLETED=50
COMPLETED_COUNT=$(yq eval '.completed_log | length' "$HANDOVER_FILE" 2>/dev/null || echo 0)
if [ "$COMPLETED_COUNT" -gt "$MAX_COMPLETED" ]; then
    START_INDEX=$((COMPLETED_COUNT - MAX_COMPLETED))
    yq eval -i ".completed_log |= .[$START_INDEX:]" "$HANDOVER_FILE"
    echo "[SYSTEM] Trimmed $((COMPLETED_COUNT - MAX_COMPLETED)) oldest completed_log entries"
fi

# Release lock (trap also handles this on abnormal exit)
exec 200>&-

echo "[SYSTEM] Handover merged successfully."

# === 8. Cleanup on Success ===
rm -f "${LOG_FILE}" "${RAW_LOG}" "${RAW_LOG}.patch"
echo "[SYSTEM] Temporary files cleaned up."
echo "[SYSTEM] Backup of previous handover: ${HANDOVER_FILE}.bak"

exit $AGENT_EXIT
