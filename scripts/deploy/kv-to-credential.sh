#!/bin/bash
# Status: production
# Path: systemd:cashbook.service ExecStartPre (and future Stage 3 services)
# kv-to-credential.sh — fetch ONE Key Vault secret into a mode-600 file.
#
# Why a file instead of env:
#   Stage 2 injected every KV secret into the process environment via
#   kv-fetch-env.py, so they were readable from /proc/<pid>/environ, `ps e`,
#   and `podman exec <c> env` (measured: cashbook had 121 env vars incl. all
#   KV secrets on 2026-09-21). Stage 3 passes a single secret as a file the app
#   reads directly, so it never enters the environment.
#
# IMPORTANT — do NOT pair this with systemd LoadCredential= in the same unit:
#   systemd resolves LoadCredential= BEFORE ExecStartPre= runs, so the source
#   file does not exist yet and the unit fails with status=243/CREDENTIALS
#   (verified 2026-09-21 on systemd 252). The correct pattern is: ExecStartPre
#   writes the file, and the app reads it via an env-var *path* (the path is
#   not secret). See docs/security/secret-injection-hardening.md.
#
# Usage: kv-to-credential.sh <KV-NAME> <output-file>
set -euo pipefail

KV_NAME="${1:?usage: kv-to-credential.sh <KV-NAME> <output-file>}"
OUT_FILE="${2:?usage: kv-to-credential.sh <KV-NAME> <output-file>}"
DEPLOY_DIR="/opt/projects/server/scripts/deploy"
ENV_NAME="$(printf '%s' "$KV_NAME" | /usr/bin/tr '-' '_' | /usr/bin/tr '[:lower:]' '[:upper:]')"

# kv-fetch-env.py `env --keys` prints raw "NAME=value" lines (newlines collapsed).
# --keys enforces least privilege and fails loudly if the key is missing.
# stderr carries the status line and must not pollute the value.
value="$(python3 "$DEPLOY_DIR/kv-fetch-env.py" env --keys "$KV_NAME" 2>/dev/null \
    | sed -n "s/^${ENV_NAME}=//p")"

if [ -z "$value" ]; then
    echo "kv-to-credential: failed to fetch '$KV_NAME' from Key Vault" >&2
    exit 1
fi

install -d -m 700 "$(dirname "$OUT_FILE")"
(umask 077; printf '%s' "$value" > "$OUT_FILE")
chmod 600 "$OUT_FILE"
echo "kv-to-credential: wrote $KV_NAME to $OUT_FILE" >&2
