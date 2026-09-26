#!/bin/bash
# Status: production
# Path: scheduled via systemd-run (one-shot P0) — see docs/plans/watchdog-cutover-execution-20260926.md
# watchdog-shadow-final-parity.sh — P0 gate: capture v2 window integrity + final parity.
#
# [WHY] The 24h shadow window is the cutover evidence. This runs read-only right
# after the window closes so the parity result is captured without a human in the
# loop. It never restarts v2 or legacy: a restart resets the window and voids the
# evidence (see plan C1).
set -uo pipefail

WINDOW_START="2026-09-25T04:10:17Z"
LOG="/opt/projects/server/logs/watchdog-shadow-final-parity.log"
TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
  echo "=== watchdog shadow final parity @ ${TS} ==="
  systemctl --user show devforge-watchdog-v2.service \
    -p NRestarts -p ExecMainStartTimestamp -p ActiveState -p SubState
  echo "--- parity (since ${WINDOW_START}) ---"
} >> "$LOG" 2>&1

/usr/bin/python3.12 /opt/projects/server/scripts/watchdog_parity.py \
  --since "$WINDOW_START" >> "$LOG" 2>&1
rc=$?
echo "parity_exit=${rc}" >> "$LOG"

if [ "$rc" -eq 0 ]; then
  echo "P0 GATE: PASS — detection_gaps=0, legacy_only=0. Cutover (P1) needs user approval." >> "$LOG"
else
  echo "P0 GATE: FAIL — inspect parity output above; hold cutover." >> "$LOG"
fi
echo "--- end ${TS} ---" >> "$LOG"
exit "$rc"
