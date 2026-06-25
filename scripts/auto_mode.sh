#!/bin/bash
# auto_mode.sh — DevForge Auto Mode (overnight unattended task execution)
# Reads auto_tasks.md, executes each ## task via Claude Code.
# Full permissions, no interruptions, all output logged.
#
# Triggered by: systemctl --user start devforge-auto.service
# Or manually:   bash scripts/auto_mode.sh

set -o pipefail

LOG_TS() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

TASK_FILE="/opt/projects/server/data/auto_tasks.md"
LOG_DIR="/opt/projects/server/data/auto_logs"
TIMESTAMP=$(date -u +%Y%m%d_%H%M%SZ)
SESSION_LOG="$LOG_DIR/auto_${TIMESTAMP}.log"
STATUS_FILE="/opt/projects/server/data/auto_status.yaml"
CLAUDE_BIN="${CLAUDE_BIN:-/home/opc/.local/bin/claude}"

mkdir -p "$LOG_DIR"

log() { echo "[$(LOG_TS)] $*" | tee -a "$SESSION_LOG"; }

# Global counters (set by main, read by _run_task)
TOTAL_TASKS=0
OK_COUNT=0
FAIL_COUNT=0
TASK_RESULTS=()

# ═══════════════════════════════════════════════════════════════
# Pre-flight memory check — prevent OOM before task execution
# ═══════════════════════════════════════════════════════════════

_ensure_memory() {
    local min_avail_mb="${1:-4096}"
    local avail_mb
    avail_mb=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)

    if [ "$avail_mb" -ge "$min_avail_mb" ]; then
        return 0
    fi

    log "  mem: available ${avail_mb}MB < ${min_avail_mb}MB — running cleanup..."
    systemctl --user restart container-devforge-pod-b.service 2>/dev/null || true
    sleep 30

    local swap_used
    swap_used=$(awk '/SwapTotal/ {t=$2} /SwapFree/ {f=$2} END {printf "%d", (t-f)/1024}' /proc/meminfo 2>/dev/null || echo 0)
    if [ "$swap_used" -gt 3700 ]; then
        log "  mem: swap ${swap_used}MB nearly full — swapoff/swapon..."
        sudo swapoff -a 2>/dev/null && sudo swapon -a 2>/dev/null || true
        sleep 10
    fi

    avail_mb=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
    log "  mem: available ${avail_mb}MB after cleanup"
}

# ═══════════════════════════════════════════════════════════════
# Execute a single task via Claude Code
# ═══════════════════════════════════════════════════════════════

_run_task() {
    local num="$1" title="$2" body="$3"
    local task_log="$LOG_DIR/task_${TIMESTAMP}_${num}.log"

    # Strip blank lines from body
    body=$(echo "$body" | sed '/^$/d')

    if [ -z "$body" ]; then
        log "  [$num/$TOTAL_TASKS] SKIP: '$title' — empty body"
        TASK_RESULTS+=("- [$num] **$title** — SKIPPED (empty body)")
        return 0
    fi

    _ensure_memory 4096

    local body_short="${body:0:120}..."
    log "  [$num/$TOTAL_TASKS] START: $title"
    log "    prompt: $body_short"

    local prompt="Task: $title

Instructions:
$body

IMPORTANT: Work autonomously. Do NOT ask for confirmation. Make decisions yourself. If you encounter errors, fix them. Complete the task fully before responding."

    local exit_code=0

    # Run Claude Code non-interactive with full permissions
    $CLAUDE_BIN -p "$prompt" \
        --permission-mode bypassPermissions \
        --dangerously-skip-permissions \
        --output-format text \
        --no-session-persistence \
        > "$task_log" 2>&1 || exit_code=$?

    if [ "$exit_code" -eq 0 ]; then
        local output_lines
        output_lines=$(wc -l < "$task_log")
        log "    [$num/$TOTAL_TASKS] OK (${output_lines} lines output)"
        TASK_RESULTS+=("- [$num] **$title** — OK (${output_lines} lines)")
        ((OK_COUNT++))
    else
        log "    [$num/$TOTAL_TASKS] FAILED (exit code $exit_code)"
        TASK_RESULTS+=("- [$num] **$title** — FAILED (exit=$exit_code)")
        ((FAIL_COUNT++))

        # Append last 20 lines of error output to session log
        log "    --- last 20 lines of output ---"
        tail -20 "$task_log" | while IFS= read -r err_line; do
            log "    | $err_line"
        done
        log "    --- end ---"
    fi
}

# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

main() {
    # ── Validation ─────────────────────────────────────────────

    if [ ! -f "$TASK_FILE" ]; then
        log "ERROR: Task file not found: $TASK_FILE"
        log "Create it with ## headings for each task, e.g.:"
        log "  ## Fix the login bug"
        log "  Review and fix the login timeout issue in auth.py..."
        exit 1
    fi

    TASK_SIZE=$(wc -c < "$TASK_FILE")
    if [ "$TASK_SIZE" -lt 10 ]; then
        log "INFO: Task file is empty — nothing to do"
        exit 0
    fi

    # ── Parse tasks ────────────────────────────────────────────

    TEMP_DIR=$(mktemp -d)
    trap "rm -rf $TEMP_DIR" EXIT

    # Parse tasks: skip HTML comments (<!-- -->) and Example: headings
    awk '
    BEGIN { in_comment = 0 }
    /<!--/ { in_comment = 1 }
    /-->/  { in_comment = 0; next }
    { if (in_comment) next }
    /^## / {
        if (task_num > 0) { printf("\n__AUTO_TASK_END__\n") }
        task_num++
        sub(/^## /, "")
        title = $0
        # Skip Example: tasks from template
        if (title ~ /^Example:/) { task_num--; next }
        printf("__AUTO_TASK_%d__\n%s\n", task_num, title)
        next
    }
    { print }
    END { if (task_num > 0) printf("\n__AUTO_TASK_END__\n") }
    ' "$TASK_FILE" > "$TEMP_DIR/parsed.md"

    TOTAL_TASKS=$(grep -cE "^__AUTO_TASK_[0-9]+__$" "$TEMP_DIR/parsed.md" 2>/dev/null || true)
    TOTAL_TASKS="${TOTAL_TASKS//[^0-9]/}"
    TOTAL_TASKS="${TOTAL_TASKS:-0}"
    log "Found $TOTAL_TASKS task(s) in $TASK_FILE"

    if [ "$TOTAL_TASKS" -eq 0 ]; then
        log "INFO: No ## headings found — nothing to do"
        exit 0
    fi

    # ── Execute tasks ──────────────────────────────────────────

    CURRENT_TITLE=""
    CURRENT_BODY=""
    IN_BODY=false
    TASK_NUM=0

    while IFS= read -r line; do
        if [[ "$line" =~ ^__AUTO_TASK_([0-9]+)__$ ]]; then
            if [ "$TASK_NUM" -gt 0 ] && [ -n "$CURRENT_BODY" ]; then
                _run_task "$TASK_NUM" "$CURRENT_TITLE" "$CURRENT_BODY"
            fi
            TASK_NUM="${BASH_REMATCH[1]}"
            CURRENT_TITLE=""
            CURRENT_BODY=""
            IN_BODY=false
        elif [[ "$line" == "__AUTO_TASK_END__" ]]; then
            if [ "$TASK_NUM" -gt 0 ] && [ -n "$CURRENT_BODY" ]; then
                _run_task "$TASK_NUM" "$CURRENT_TITLE" "$CURRENT_BODY"
            fi
            TASK_NUM=0
            CURRENT_TITLE=""
            CURRENT_BODY=""
            IN_BODY=false
        elif [ "$IN_BODY" = false ]; then
            CURRENT_TITLE="$line"
            IN_BODY=true
        else
            if [ -z "$CURRENT_BODY" ]; then
                CURRENT_BODY="$line"
            else
                CURRENT_BODY="$CURRENT_BODY"$'\n'"$line"
            fi
        fi
    done < "$TEMP_DIR/parsed.md"

    # Last task (if no __AUTO_TASK_END__ marker)
    if [ "$TASK_NUM" -gt 0 ] && [ -n "$CURRENT_BODY" ]; then
        _run_task "$TASK_NUM" "$CURRENT_TITLE" "$CURRENT_BODY"
    fi

    # ── Archive + reset ────────────────────────────────────────

    ARCHIVE_FILE="${TASK_FILE%.md}_${TIMESTAMP}.md"
    cp "$TASK_FILE" "$ARCHIVE_FILE"

    {
        echo ""
        echo "---"
        echo "## Auto Mode Results — $(LOG_TS)"
        echo ""
        for entry in "${TASK_RESULTS[@]}"; do
            echo "$entry"
        done
        echo ""
        echo "**Total:** $TOTAL_TASKS tasks | **OK:** $OK_COUNT | **Failed:** $FAIL_COUNT"
    } >> "$ARCHIVE_FILE"

    # Reset task file with template
    cat > "$TASK_FILE" << 'TEMPLATE'
# Auto Tasks

<!-- Write tasks below using ## headings. One task per heading.
     Tasks execute overnight via devforge-auto.timer (01:00 KST).
     Each ## section = a separate Claude Code invocation.
     Full permissions granted. Results logged to auto_logs/.
     Empty file = nothing to do.

## Example: Fix the login timeout
Review auth.py and fix the 30s timeout issue. Check error handling too.

## Example: Update dependencies
Run pip install --upgrade for all packages and fix any breaking changes.
-->

TEMPLATE

    cat > "$STATUS_FILE" <<YAML
timestamp: "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
last_run_log: "$SESSION_LOG"
total_tasks: $TOTAL_TASKS
ok: $OK_COUNT
failed: $FAIL_COUNT
YAML

    log "Auto mode complete: $OK_COUNT/$TOTAL_TASKS OK"

    if [ "$FAIL_COUNT" -gt 0 ]; then
        log "WARNING: $FAIL_COUNT task(s) failed — check $SESSION_LOG"
        return 1
    fi
    return 0
}

main "$@"
