#!/bin/bash
# Launch Gemini CLI in the foreground with per-request key rotation.
# Default model: gemini-2.5-flash (classification + simple tasks)
# Use /model gemma-4-26b-a4b-it (plan) or /model gemma-4-31b-a4b-it (code) inside Gemini.
#
# Usage:
#   /opt/projects/server/scripts/gemini_session_start.sh          # start
#   /opt/projects/server/scripts/gemini_session_start.sh --prompt "..."  # headless run

set -euo pipefail

SESSION_NAME="gemini"
DEFAULT_MODEL="gemini-2.5-flash"

if [ -z "${ENCRYPTION_PASSPHRASE:-}" ]; then
    echo "오류: ENCRYPTION_PASSPHRASE가 설정되지 않았습니다." >&2
    exit 1
fi

start_session() {
    local api_key=$(python3 -c "
import json, os, sys
from lib.auth.key_loader import load_api_keys, STATE_FILE
from lib.key_rotator import KeyRotator

keys = load_api_keys()
if not keys:
    print('ERROR:no keys', file=sys.stderr)
    sys.exit(1)
r = KeyRotator(keys)
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        r._calls = {int(k): v for k, v in s.get('calls', {}).items()}
        r._fails = {int(k): v for k, v in s.get('fails', {}).items()}
        r._last_used = {int(k): v for k, v in s.get('last_used', {}).items()}
        r._backoff_until = {int(k): v for k, v in s.get('backoff_until', {}).items()}
    except Exception:
        pass
picked = r.pick()
if picked is None:
    print('ERROR:all keys backoff', file=sys.stderr)
    sys.exit(1)
idx, key_name, key = picked
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
state = {'calls': r._calls, 'fails': r._fails, 'last_used': r._last_used, 'backoff_until': r._backoff_until}
with open(STATE_FILE + '.tmp', 'w') as f:
    json.dump(state, f)
os.rename(STATE_FILE + '.tmp', STATE_FILE)
print(key)
print(key_name, file=sys.stderr)
")

    local key=$(echo "$api_key" | tail -1)
    local key_name=$(echo "$api_key" | tail -2 | head -1)

    if [ -z "$key" ]; then
        echo "[$SESSION_NAME] 오류: 키를 가져올 수 없습니다." >&2
        return 1
    fi

    echo "[$SESSION_NAME] 시작: $key_name → $DEFAULT_MODEL"

    local log_dir="$HOME/.cache/devforge"
    local log_file="$log_dir/gemini-session.log"
    mkdir -p "$log_dir"

    local gemini_cmd
    gemini_cmd=$(printf '%q ' /home/opc/.local/bin/gemini -m "$DEFAULT_MODEL" --skip-trust "$@")

    if command -v script >/dev/null 2>&1; then
        ENCRYPTION_PASSPHRASE="$ENCRYPTION_PASSPHRASE" \
        GEMINI_API_KEY="$key" \
        GEMINI_CLI_TRUST_WORKSPACE=true \
        NODE_EXTRA_CA_CERTS=/home/opc/.local/share/devforge/certs/proxy-cert.pem \
            script -q -e -f -a "$log_file" -c "$gemini_cmd" /dev/null
    else
        ENCRYPTION_PASSPHRASE="$ENCRYPTION_PASSPHRASE" \
        GEMINI_API_KEY="$key" \
        GEMINI_CLI_TRUST_WORKSPACE=true \
        NODE_EXTRA_CA_CERTS=/home/opc/.local/share/devforge/certs/proxy-cert.pem \
            /home/opc/.local/bin/gemini -m "$DEFAULT_MODEL" --skip-trust "$@" 2>&1 | tee -a "$log_file"
        return ${PIPESTATUS[0]}
    fi
}

case "${1:-start}" in
    start)  shift; start_session "$@" ;;
    *)      start_session "$@" ;;
esac
