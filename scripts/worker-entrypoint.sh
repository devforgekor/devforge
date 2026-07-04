#!/bin/bash
# worker-entrypoint.sh — DevForge Worker supervisor runner
set -e

export DEVFORGE_DB_TCP=1

echo "[worker-entrypoint] Starting worker supervisor..."
exec python3 /scripts/worker_supervisor.py
