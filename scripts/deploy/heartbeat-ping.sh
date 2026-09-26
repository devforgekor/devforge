#!/bin/bash
# Status: production
# Path: systemd ExecStartPost — see docs/plans/fitness-functions-heartbeat-drift-guide.md
# heartbeat-ping.sh <job> — dead-man's switch ping.
# Upserts heartbeat_<job> in watchdog_pulses (created_at=now) so watchdog v2's
# HeartbeatHealthChecker can alert when the job silently stops running.
set -uo pipefail

JOB="${1:?usage: heartbeat-ping.sh <job>}"
PYTHONPATH=/opt/projects/server/scripts /usr/bin/python3.12 -c \
  'import sys; from lib.watchdog.messenger import heartbeat; raise SystemExit(0 if heartbeat(sys.argv[1]) else 1)' \
  "$JOB"
