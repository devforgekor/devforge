#!/bin/bash
# kuhwa-schedule 실행 래퍼: KV 시크릿을 최소 주입하고 코드 기대 이름으로 alias.
#
# KV 키(KUWHA-SMTP-*)는 저장소 단일 규칙(DASH/UPPER)을 따르고, kuhwa 코드는
# SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/MAIL_TO 를 읽으므로 여기서 매핑한다.
set -euo pipefail

VAULT_KV="/opt/projects/server/scripts/deploy/kv-export-env.sh"
ENV_FILE="/run/user/$(id -u)/kv-kuhwa.env"
KUWHA_DIR="/opt/workspace/minihome/apps/kuhwa"
NODE_BIN="/home/opc/.local/bin/node"

KEYS="NEIS-API-KEY,KUWHA-SMTP-HOST,KUWHA-SMTP-PORT,KUWHA-SMTP-USER,KUWHA-SMTP-PASSWORD,KUWHA-MAIL-TO"

cleanup() { rm -f "$ENV_FILE"; }
trap cleanup EXIT

"$VAULT_KV" "$ENV_FILE" "$KEYS"

# 값에 공백(Gmail 앱 비밀번호 'xxxx xxxx xxxx xxxx')이 있어 shell source는 부적합.
# 라인 단위로 KEY=VALUE 를 읽어 export 한다(shell 해석 없음).
while IFS= read -r line; do
    case "$line" in
        ''|'#'*) continue ;;
    esac
    key=${line%%=*}
    val=${line#*=}
    export "$key=$val"
done < "$ENV_FILE"

# 코드 기대 이름으로 alias (값 복사, KV 키는 그대로 두지 않음)
export SMTP_HOST="${KUWHA_SMTP_HOST:-}"
export SMTP_PORT="${KUWHA_SMTP_PORT:-587}"
export SMTP_USER="${KUWHA_SMTP_USER:-}"
export SMTP_PASSWORD="${KUWHA_SMTP_PASSWORD:-}"
export MAIL_TO="${KUWHA_MAIL_TO:-${KUWHA_SMTP_USER:-}}"

unset KUWHA_SMTP_HOST KUWHA_SMTP_PORT KUWHA_SMTP_USER KUWHA_SMTP_PASSWORD KUWHA_MAIL_TO

cd "$KUWHA_DIR"
exec "$NODE_BIN" scripts/fetch-schedule.js
