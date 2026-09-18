#!/bin/bash
# Key Vault → 임시 env 파일 생성 (컨테이너용)
# 사용법: kv-export-env.sh [output_path]
set -euo pipefail

OUTPUT=${1:-/run/user/$(id -u)/kv-temp.env}

# Key Vault에서 시크릿 로드하여 env 파일 형식으로 출력
python3 "$(dirname "$0")/kv-fetch-env.py" env > "$OUTPUT"

# 권한 설정
chmod 600 "$OUTPUT"

echo "✅ Key Vault 시크릿을 $OUTPUT 에 저장" >&2
