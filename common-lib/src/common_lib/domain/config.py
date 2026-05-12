#!/usr/bin/env python3
"""domain/config | load .env file, get required env vars with defaults, validate prod secrets | load_env_file(),get_required_env(),validate_production_secrets()"""

import os
import sys
from pathlib import Path

def load_env_file(env_path: Path) -> None:
    """
    .env 파일을 읽어 환경 변수에 설정합니다.

    매개변수:
        env_path: .env 파일 경로
    """
    if not env_path.is_file():
        return

    import re
    with open(env_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            match = re.match(r'^\s*([\w\.]+)\s*=\s*(.*?)\s*$', line)
            if match:
                key, value = match.groups()
                # 값에서 후행 주석 제거
                value = value.split('#')[0].strip()
                os.environ[key] = value

def get_required_env(var_name: str, default: str = None) -> str:
    """
    필수 환경 변수를 가져옵니다. 없으면 오류를 발생시킵니다.

    매개변수:
        var_name: 환경 변수 이름
        default: 기본값 (지정하면 오류 대신 기본값 반환)

    반환값:
        환경 변수 값
    """
    value = os.getenv(var_name)
    if value is None:
        if default is not None:
            return default
        print(f"[ERROR] 필수 환경 변수가 설정되지 않았습니다: {var_name}", file=sys.stderr)
        sys.exit(1)
    return value

def validate_production_secrets() -> None:
    """
    프로덕션 모드에서 필요한 환경 변수가 모두 설정되었는지 검증합니다.
    """
    required = [
        "AZURE_OPENAI_ENDPOINT",
        "API_KEY_AI",
        "API_KEY_SMS",
        "API_SECRET_SMS",
        "SMS_SENDER_PHONE",
    ]
    missing = [var for var in required if not os.getenv(var)]
    if missing:
        print("\n오류: 다음 환경변수가 설정되지 않았습니다.", file=sys.stderr)
        for var in missing:
            print(f"  - {var}", file=sys.stderr)
        print("\n사용법:", file=sys.stderr)
        print("  export AZURE_OPENAI_ENDPOINT=\"https://...openai.azure.com/\"", file=sys.stderr)
        print("  export API_KEY_AI=\"your-api-key\"", file=sys.stderr)
        print("  export API_KEY_SMS=\"your-sms-key\"", file=sys.stderr)
        print("  export API_SECRET_SMS=\"your-sms-secret\"", file=sys.stderr)
        print("  export SMS_SENDER_PHONE=\"01012345678\"", file=sys.stderr)
        sys.exit(1)
    print("프로덕션 환경 필수값 확인 완료", file=sys.stderr)

if __name__ == "__main__":
    # 간단한 테스트
    print("domain/config.py 테스트")
    print(f"현재 디렉토리: {Path.cwd()}")
    env_file = Path(".env")
    if env_file.is_file():
        load_env_file(env_file)
        print(".env 파일 로드 완료")
    else:
        print(".env 파일 없음")
    sys.exit(0)