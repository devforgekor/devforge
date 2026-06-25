#!/bin/bash
# Watchdog for 5-phase pipeline — polls log, sends Slack on phase changes.
# Usage: nohup bash watchdog_pipeline.sh <log_file> &

LOG_FILE="$1"
SLACK_SECRETS="$HOME/.config/devforge/secrets.env"
POLL_SECONDS=120
PHASE_MAP=(
    "Phase 0: Boot"
    "Phase 1: Python Verify"
    "Phase 2: 7B Verify"
    "Phase 3: 7B-3B-7B Day PRJ"
    "Phase 4: Night P-R-J"
    "Phase 5: 27B IQ4_XS Verify"
    "Phase 7: Restore Day"
)

last_phase=""
last_line_count=0

send_slack() {
    local msg="$1"
    source "$SLACK_SECRETS" 2>/dev/null
    curl -s -X POST https://slack.com/api/chat.postMessage \
        -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
        -H "Content-type: application/json" \
        -d "{\"channel\":\"U0APJGD8CBW\",\"text\":\"${msg}\"}" > /dev/null 2>&1
}

# Initial status snapshot: send running phase immediately
first_phase=$(head -10 "$LOG_FILE" 2>/dev/null | grep -E "Phase [0-9]+:" | tail -1)
send_slack "[pipeline] v3.0 started - ${first_phase:-Phase 0}"

while true; do
    sleep "$POLL_SECONDS"
    [ ! -f "$LOG_FILE" ] && { send_slack "[pipeline] ERROR - log file disappeared"; break; }

    current_count=$(wc -l < "$LOG_FILE")
    last_line=$(tail -1 "$LOG_FILE")

    for phase in "${PHASE_MAP[@]}"; do
        if echo "$last_line" | grep -q "$phase"; then
            send_slack "[pipeline] Phase: $phase"
            break
        fi
    done

    # Check for error or timeout patterns
    if echo "$last_line" | grep -qi "error\|timeout\|oom\|killed\|traceback"; then
        send_slack "[pipeline] ERROR detected: $last_line"
        break
    fi

    # Check if process is still alive
    if ! pgrep -f "night.py --all" > /dev/null 2>&1; then
        send_slack "[pipeline] Completed. Final log lines:\n$(tail -5 "$LOG_FILE")"
        break
    fi

    last_line_count=$current_count
done
