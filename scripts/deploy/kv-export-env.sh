#!/bin/bash
# Key Vault → 임시 env 파일 생성 (컨테이너용)
# 사용법: kv-export-env.sh [output_path]
#
# NOTE(2026-09-19): kv-fetch-env.py `env`는 raw KEY=VALUE 를 출력한다.
# 과거 shell-quote(`KEY='value'`) 출력 시 EnvironmentFile 소비자가 따옴표를
# 값의 일부로 읽어 인증이 조용히 실패했다(WEBOBSIDIAN_PASSWORD 사례).
# 아래 normalize 단계가 quoting artifact 를 자동 감지·수정하고 형식을 검증한다.
set -euo pipefail

SCRIPT_DIR="$(dirname "$0")"
OUTPUT=${1:-/run/user/$(id -u)/kv-temp.env}

# Key Vault에서 시크릿 로드하여 env 파일 형식으로 출력
python3 "$SCRIPT_DIR/kv-fetch-env.py" env > "$OUTPUT"

# EnvironmentFile quoting 자동수정 + KEY=VALUE 형식 검증 (실패 시 잘못된 env 차단)
python3 "$SCRIPT_DIR/env-file-normalize.py" "$OUTPUT"

# 권한 설정
chmod 600 "$OUTPUT"

echo "✅ Key Vault 시크릿을 $OUTPUT 에 저장" >&2
