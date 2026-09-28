#!/bin/bash
# kuhwa-schedule 실행 래퍼: KV 시크릿을 최소 주입하고 코드 기대 이름으로 alias.
#
# SMTP 설정은 JSON 1개(GMAIL-ENV-CONFIG-*)로 통합(2026-09-28) 관리하고, kuhwa
# 코드는 SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/MAIL_TO 를 읽으므로 여기서 펼친다.
# 비밀번호만 별도 시크릿(passwordRef)으로 분리 보관한다.
set -euo pipefail

VAULT_KV="/opt/projects/server/scripts/deploy/kv-export-env.sh"
ENV_FILE="/run/user/$(id -u)/kv-kuhwa.env"
KUWHA_DIR="/opt/workspace/minihome/apps/kuhwa"
NODE_BIN="/home/opc/.local/bin/node"

KEYS="NEIS-API-KEY,GMAIL-ENV-CONFIG-MINIPARK4U,GMAIL-SMTP-PASSWORD-MINIPARK4U"

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

# JSON config를 코드 기대 이름으로 펼친다.
# [WHY] GMAIL 4개 시크릿이 GMAIL-ENV-CONFIG-1개 JSON으로 통합됐으나 kuhwa 코드는
# 개별 SMTP_* env를 읽는다 — 여기서만 alias하고 원본은 unset 한다.
eval "$(jq -er '
  "export SMTP_HOST=\(.host|@json)\nexport SMTP_PORT=\(.port|tostring)\nexport SMTP_USER=\(.user|@json)\nexport MAIL_TO=\((.mailTo // .user)|@json)"
' <<<"$GMAIL_ENV_CONFIG_MINIPARK4U")"
export SMTP_PASSWORD="${GMAIL_SMTP_PASSWORD_MINIPARK4U:-}"

unset GMAIL_ENV_CONFIG_MINIPARK4U GMAIL_SMTP_PASSWORD_MINIPARK4U

cd "$KUWHA_DIR"
exec "$NODE_BIN" scripts/fetch-schedule.js
