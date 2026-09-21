#!/bin/bash
# Status: production
# Path: none — operator convenience entry point, referenced by
#       docs/runbooks/option-4-server-maintenance-summary.md and
#       docs/runbooks/daily-server-maintenance.md
# health-check.sh — quick DevForge server health snapshot (5-minute check).
# Read-only: performs no recovery actions. Exit 1 if a CRITICAL condition is seen.
set -uo pipefail

BACKUP_GLOB="/opt/ai_data/backups/db/devforge_*.dump"
DISK_WARN=85
DISK_CRIT=90
MEM_WARN=90
BACKUP_MAX_AGE_H=24

FAIL=0

echo "=== DevForge Health Check $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# 1. Resources
echo "--- resources ---"
uptime
free -h | awk 'NR<=2'
echo "cores=$(nproc) load(1m)=$(awk '{print $1}' /proc/loadavg)"

# 2. Failed user services
echo "--- failed user services ---"
failed=$(systemctl --user list-units --type=service --state=failed --no-legend --plain 2>/dev/null)
if [ -n "$failed" ]; then
    echo "$failed"
    FAIL=$((FAIL + 1))
else
    echo "none"
fi

# 3. Recent errors (last hour)
echo "--- journal errors (1h) ---"
errs=$(journalctl --user --since "1 hour ago" --priority=err --no-pager -n 10 2>/dev/null)
if [ -n "$errs" ]; then
    echo "$errs"
else
    echo "none"
fi

# 4. Disk usage on /
echo "--- disk (/) ---"
disk_pct=$(df -P / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')
if [ "$disk_pct" -ge "$DISK_CRIT" ]; then
    echo "CRITICAL ${disk_pct}%"
    FAIL=$((FAIL + 1))
elif [ "$disk_pct" -ge "$DISK_WARN" ]; then
    echo "WARN ${disk_pct}%"
else
    echo "OK ${disk_pct}%"
fi

# 5. Memory usage
echo "--- memory ---"
mem_pct=$(free | awk 'NR==2 {printf "%.0f", $3 / $2 * 100}')
echo "used ${mem_pct}%"
if [ "$mem_pct" -ge "$MEM_WARN" ]; then
    echo "WARN memory >= ${MEM_WARN}%"
fi

# 6. Backup freshness (pg_dump -Fc custom-format dumps)
echo "--- backup ---"
newest=$(ls -1t $BACKUP_GLOB 2>/dev/null | head -1)
if [ -z "$newest" ]; then
    echo "CRITICAL no backup found ($BACKUP_GLOB)"
    FAIL=$((FAIL + 1))
else
    age_h=$(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 ))
    echo "newest=$(basename "$newest") age=${age_h}h"
    if [ "$age_h" -gt "$BACKUP_MAX_AGE_H" ]; then
        echo "WARN backup older than ${BACKUP_MAX_AGE_H}h"
    fi
fi

echo "=== Check Complete (critical=$FAIL) ==="
exit "$FAIL"
