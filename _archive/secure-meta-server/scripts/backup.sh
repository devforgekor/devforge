#!/bin/bash
# DevForge daily backup — /opt/project + PostgreSQL
set -e

BACKUP_DIR="/opt/ai_data/backups"
RETENTION_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M)
LOG_FILE="/tmp/devforge-backup.log"

mkdir -p "$BACKUP_DIR"

# 1. Project files (excluding aider-env venv)
tar -czf "${BACKUP_DIR}/project_${TIMESTAMP}.tar.gz" \
    -C /opt/project \
    --exclude 'aider-env' \
    --exclude '__pycache__' \
    --exclude '.mypy_cache' \
    --exclude '*.pyc' \
    common-lib seedling server litellm CLAUDE.md 2>/dev/null

# 2. PostgreSQL dump
sudo -u postgres pg_dump -Fc -Z4 seedling > "${BACKUP_DIR}/pg_seedling_${TIMESTAMP}.dump"

# 3. Cleanup old backups
find "${BACKUP_DIR}" -name 'project_*.tar.gz' -mtime "+${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -name 'pg_seedling_*.dump' -mtime "+${RETENTION_DAYS}" -delete

echo "[$(date -Iseconds)] Backup OK — project: $(du -h "${BACKUP_DIR}/project_${TIMESTAMP}.tar.gz" | cut -f1), pg: $(du -h "${BACKUP_DIR}/pg_seedling_${TIMESTAMP}.dump" | cut -f1)" | tee -a "$LOG_FILE"
