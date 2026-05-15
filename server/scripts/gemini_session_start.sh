#!/bin/bash
# Launch persistent Gemini CLI session in tmux with per-request key rotation.
# Default model: gemini-2.5-flash (classification + simple tasks)
# Switch within session: /model gemma-4-26b-a4b-it (plan)  /model gemma-4-31b-it (code)
#
# Usage:
#   /opt/projects/server/scripts/gemini_session_start.sh          # start
#   /opt/projects/server/scripts/gemini_session_start.sh attach   # attach

set -euo pipefail

SESSION_NAME="gemini"
DEFAULT_MODEL="gemini-2.5-flash"

if [ -z "${ENCRYPTION_PASSPHRASE:-}" ]; then
    echo "오류: ENCRYPTION_PASSPHRASE가 설정되지 않았습니다." >&2
    exit 1
fi

attach_session() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        exec tmux attach-session -t "$SESSION_NAME"
    else
        echo "세션이 없습니다. 먼저 start로 시작하세요." >&2
        exit 1
    fi
}

start_session() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "[$SESSION_NAME] 이미 실행 중" >&2
        return 0
    fi

    local api_key=$(python3 -c "
import json, os, sys
sys.path.insert(0, '/opt/projects/server')
from scripts.gemini_rotate import _load_keys, STATE_FILE
from lib.key_rotator import KeyRotator

keys = _load_keys()
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

    tmux new-session -d -s "$SESSION_NAME" \
        -e ENCRYPTION_PASSPHRASE="$ENCRYPTION_PASSPHRASE" \
        -e GEMINI_CLI_TRUST_WORKSPACE=true \
        -e NODE_EXTRA_CA_CERTS=/home/opc/.local/share/devforge/certs/proxy-cert.pem \
        bash -c "
echo '=== Gemini CLI (flash-first) ==='
echo '분리: Ctrl+B D | 접속: tmux attach -t gemini'
echo
echo '[라우팅 전략]'
echo '  간단검색/요약 → flash 그대로'
echo '  분석/기획 → /model gemma-4-26b-a4b-it'
echo '  코드수정 → /model gemma-4-31b-it'
	echo

while true; do
    API_KEY=\$(python3 -c \"
import json, os, sys
sys.path.insert(0, '/opt/projects/server')
from scripts.gemini_rotate import _load_keys, STATE_FILE
from lib.key_rotator import KeyRotator
keys = _load_keys()
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
    print('NO_KEY')
    sys.exit(1)
idx, name, key = picked
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
state = {'calls': r._calls, 'fails': r._fails, 'last_used': r._last_used, 'backoff_until': r._backoff_until}
with open(STATE_FILE + '.tmp', 'w') as f:
    json.dump(state, f)
os.rename(STATE_FILE + '.tmp', STATE_FILE)
print(key)
\")
    if [ \"\$API_KEY\" = \"NO_KEY\" ] || [ -z \"\$API_KEY\" ]; then
        echo '키 선택 실패. 10초 후 재시도...'
        sleep 10
        continue
    fi
    echo \"[turn] \$(date +%H:%M:%S)\"
    GEMINI_API_KEY=\"\$API_KEY\" /home/opc/.local/bin/gemini -m $DEFAULT_MODEL --skip-trust
    echo
    echo '--- 새 키 로테이션 ---'
    sleep 1
done
"
    echo "[$SESSION_NAME] tmux 세션 생성 완료"
}

case "${1:-start}" in
    attach) attach_session ;;
    start)  start_session ;;
    *)      echo "사용법: $0 [start|attach]" >&2; exit 1 ;;
esac
